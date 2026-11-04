"""The verified copy engine.

For every file: copy to ``name.cspartial`` while hashing the source as it is read, fsync, read
the destination back (cache-bypassing) and hash it independently, check the source did not
change, then atomically rename into place. A file never appears under its final name unless
the bytes on the destination matched the bytes read from the source.

The engine knows nothing about Gmail, the database or the UI. Callers pass paths that have
already been checked against the configured roots (see ``contentshare.security.paths``).
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

from contentshare.copy_engine.hashing import manifest_line, manifest_suffix, new_hasher
from contentshare.copy_engine.models import (
    Control,
    CopyCancelledError,
    CopyOptions,
    FatalCopyError,
    FileEntry,
    FileOutcome,
    FileResult,
    HashMismatchError,
    Hooks,
    Plan,
    PreflightError,
    Progress,
    ProgressCallback,
    RetryableCopyError,
    SourceChangedError,
    TreeResult,
)
from contentshare.copy_engine.winio import extended, free_space, hash_file, is_reparse_or_link
from contentshare.security.paths import PathSafetyError, check_component

log = logging.getLogger(__name__)

PARTIAL_SUFFIX = ".cspartial"
_MAX_PATH_EXT = 32_000
_DISK_FULL_WINERRORS = {39, 112}  # ERROR_HANDLE_DISK_FULL, ERROR_DISK_FULL


def format_bytes(n: float) -> str:
    if abs(n) < 1024:
        return f"{n:.0f} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
    raise AssertionError("unreachable")


def _real(root: Path, rel: str) -> Path:
    return root.joinpath(*rel.split("/"))


# --------------------------------------------------------------------------- planning
def build_plan(src: Path, dst: Path) -> Plan:
    """Scan ``src`` (a folder or a single file). Links and junctions are listed as warnings and
    never followed. Names Windows cannot store, and names that differ only by case, are
    recorded as problems that block the copy."""
    if src.is_file():
        plan = Plan(src=src.parent, dst=dst)
        st = src.stat()
        _check_name(plan, src.name, src.name)
        plan.files.append(FileEntry(src.name, st.st_size, st.st_mtime_ns))
        return plan

    plan = Plan(src=src, dst=dst)
    if not src.is_dir():
        plan.problems.append(f"Source not found: {src}")
        return plan

    seen: dict[str, str] = {}
    stack = [""]
    while stack:
        rel_dir = stack.pop()
        for entry in _list_dir(plan, rel_dir):
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            _check_name(plan, entry.name, rel)
            folded = rel.casefold()
            if folded in seen:
                plan.problems.append(f"Names differ only by case: {seen[folded]} and {rel}")
            seen[folded] = rel
            _classify_entry(plan, entry, rel, stack)
    plan.files.sort(key=lambda f: f.rel)
    plan.dirs.sort()
    return plan


def _list_dir(plan: Plan, rel_dir: str) -> list[os.DirEntry[str]]:
    folder = _real(plan.src, rel_dir) if rel_dir else plan.src
    try:
        with os.scandir(extended(folder)) as it:
            return sorted(it, key=lambda e: e.name)
    except OSError as exc:
        plan.problems.append(f"Cannot read folder {rel_dir or plan.src}: {exc}")
        return []


def _classify_entry(plan: Plan, entry: os.DirEntry[str], rel: str, stack: list[str]) -> None:
    try:
        if is_reparse_or_link(entry):
            plan.warnings.append(f"Skipped link/junction (not followed): {rel}")
        elif entry.is_dir(follow_symlinks=False):
            plan.dirs.append(rel)
            stack.append(rel)
        elif entry.is_file(follow_symlinks=False):
            st = entry.stat(follow_symlinks=False)
            plan.files.append(FileEntry(rel, st.st_size, st.st_mtime_ns))
        else:
            plan.warnings.append(f"Skipped special file: {rel}")
    except OSError as exc:
        plan.problems.append(f"Cannot read {rel}: {exc}")


def _check_name(plan: Plan, name: str, rel: str) -> None:
    try:
        check_component(name)
    except PathSafetyError:
        plan.problems.append(f"Name is not valid on Windows and cannot be copied safely: {rel}")


def manifest_path(plan: Plan, algorithm: str) -> Path:
    return plan.dst / f"{plan.dst.name}.checksums{manifest_suffix(algorithm)}"


def _bytes_needed(plan: Plan, options: CopyOptions, problems: list[str]) -> int:
    needed = 0
    for entry in plan.files:
        target = extended(_real(plan.dst, entry.rel))
        if len(target) > _MAX_PATH_EXT:
            problems.append(f"Destination path is too long: {entry.rel}")
        try:
            if os.stat(target).st_size == entry.size and entry.rel not in options.overwrite:
                continue  # probably identical: will be hash-compared, needs no new space
        except OSError:
            pass
        needed += entry.size
    return int(needed * (1 + options.free_space_margin_pct / 100))


def _writable_problem(dst: Path) -> str | None:
    try:
        os.makedirs(extended(dst), exist_ok=True)
        probe = extended(dst / f".cs-write-test{PARTIAL_SUFFIX}")
        with open(probe, "wb"):
            pass
        os.unlink(probe)  # our own probe file
    except OSError as exc:
        return f"Destination is not writable ({dst}): {exc}"
    return None


def preflight(plan: Plan, options: CopyOptions, *, create_dirs: bool = True) -> None:
    """Raise :class:`PreflightError` listing everything wrong, before any byte is copied.
    With ``create_dirs=False`` (dry run) nothing is created on disk."""
    problems = list(plan.problems)
    if not plan.src.exists():
        problems.append(f"Source not found: {plan.src}")

    ancestor = plan.dst
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if not ancestor.is_dir():
        problems.append(f"Destination location is not a folder: {ancestor}")

    mname = manifest_path(plan, options.algorithm).name.casefold()
    if options.write_checksum_file and any(f.rel.casefold() == mname for f in plan.files):
        problems.append(f"A source file is named like the checksum file: {mname}")

    needed = _bytes_needed(plan, options, problems)
    if not problems:
        free = free_space(plan.dst)
        if needed > free:
            problems.append(
                f"Not enough free space at the destination: need {format_bytes(needed)}, "
                f"have {format_bytes(free)}"
            )
    if not problems and create_dirs and (blocked := _writable_problem(plan.dst)):
        problems.append(blocked)

    if problems:
        raise PreflightError(problems)


# --------------------------------------------------------------------------- progress
class _Tracker:
    def __init__(self, total: int, callback: ProgressCallback | None) -> None:
        self.total = total
        self.copied = 0
        self.verified = 0
        self._cb = callback
        self._last_emit = 0.0
        self._last_t = time.monotonic()
        self._last_bytes = 0
        self._speed = 0.0
        self._stage = ""

    def emit(self, rel: str, stage: str, done: int, size: int, *, force: bool = False) -> None:
        if self._cb is None:
            return
        now = time.monotonic()
        force = force or stage != self._stage  # a new stage is always reported
        self._stage = stage
        if not force and now - self._last_emit < 0.1:
            return
        moved = self.copied + self.verified
        dt = now - self._last_t
        if dt >= 0.05:
            instant = (moved - self._last_bytes) / dt
            self._speed = instant if self._speed == 0 else 0.7 * self._speed + 0.3 * instant
            self._last_t, self._last_bytes = now, moved
        remaining = max(0, 2 * self.total - moved)
        eta = remaining / self._speed if self._speed > 0 else None
        self._last_emit = now
        report = Progress(
            rel, stage, done, size, self.copied, self.verified, self.total, self._speed / 2, eta
        )
        try:
            self._cb(report)
        except Exception:
            log.exception("progress callback failed")


# --------------------------------------------------------------------------- single file
def _is_disk_full(exc: OSError) -> bool:
    return exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) in _DISK_FULL_WINERRORS


def _fatal_reason(exc: Exception) -> str | None:
    """Failures that retrying cannot fix."""
    if isinstance(exc, FatalCopyError):
        return str(exc)
    if isinstance(exc, OSError) and _is_disk_full(exc):
        return "The destination disk is full"
    return None


def _quiet_unlink(path: str) -> None:
    """Delete one of our own ``*.cspartial`` temp files. This is the only place the engine
    deletes anything, and it refuses any other name, so a bug elsewhere can never remove a
    real file."""
    if not path.endswith(PARTIAL_SUFFIX):
        log.error("refused to delete a file that is not a temporary copy: %s", Path(path).name)
        return
    with contextlib.suppress(OSError):
        os.unlink(path)


def _finish(
    result: FileResult, outcome: FileOutcome, error: str | None = None, digest: str | None = None
) -> FileResult:
    result.outcome, result.error, result.hash_hex = outcome, error, digest
    return result


def _write_all(handle: BinaryIO, view: memoryview) -> None:
    offset = 0
    while offset < len(view):
        written = handle.write(view[offset:])
        if not written:
            raise OSError(errno.EIO, "Write returned 0 bytes")
        offset += written


class _FileCopier:
    """Copies one file at a time with the full verify-then-rename protocol."""

    def __init__(
        self,
        plan: Plan,
        options: CopyOptions,
        control: Control,
        tracker: _Tracker,
        hooks: Hooks,
        sleep: Callable[[float], None],
    ) -> None:
        self.plan, self.opt, self.control = plan, options, control
        self.tracker, self.hooks, self.sleep = tracker, hooks, sleep

    def copy(self, entry: FileEntry) -> FileResult:
        src = extended(_real(self.plan.src, entry.rel))
        dst = extended(_real(self.plan.dst, entry.rel))
        result = FileResult(entry.rel, FileOutcome.FAILED, entry.size, entry.mtime_ns)
        if (early := self._triage_existing(entry, src, dst, result)) is not None:
            return early
        return self._copy_with_retries(entry, src, dst, result)

    # -- what is already at the destination?
    def _triage_existing(
        self, entry: FileEntry, src: str, dst: str, result: FileResult
    ) -> FileResult | None:
        """Return a finished result if nothing needs copying, or ``None`` to go ahead."""
        try:
            existing = os.stat(dst)
        except FileNotFoundError:
            return None
        except OSError as exc:
            return _finish(result, FileOutcome.FAILED, f"Cannot inspect destination: {exc}")

        if not stat.S_ISREG(existing.st_mode):
            return _finish(
                result, FileOutcome.CONFLICT,
                "A folder or special item already has this name at the destination",
            )  # fmt: skip

        if (done := self._already_verified(entry, src, dst, existing, result)) is not None:
            return done

        if entry.rel in self.opt.overwrite:
            return None  # the user agreed to replace it; the final rename uses os.replace
        return _finish(
            result, FileOutcome.CONFLICT,
            "A different file with this name already exists at the destination",
        )  # fmt: skip

    def _already_verified(
        self, entry: FileEntry, src: str, dst: str, existing: os.stat_result, result: FileResult
    ) -> FileResult | None:
        """Skip the copy if a previous run verified this file, or the bytes are identical."""
        trusted = self.opt.trusted.get(entry.rel)
        if (
            trusted
            and existing.st_size == trusted.size == entry.size
            and existing.st_mtime_ns == trusted.mtime_ns == entry.mtime_ns
        ):
            self.tracker.copied += entry.size
            self.tracker.verified += entry.size
            return _finish(result, FileOutcome.SKIPPED_TRUSTED, digest=trusted.hash_hex)

        if existing.st_size == entry.size:
            try:
                same, digest = self._identical(src, dst, entry)
            except OSError as exc:
                return _finish(
                    result, FileOutcome.FAILED, f"Cannot compare with existing file: {exc}"
                )
            if same:
                return _finish(result, FileOutcome.SKIPPED_IDENTICAL, digest=digest)
        return None

    def _identical(self, src: str, dst: str, entry: FileEntry) -> tuple[bool, str]:
        def counted(n: int) -> None:
            self.tracker.verified += n
            self.tracker.emit(entry.rel, "verify", 0, entry.size)

        a, _ = hash_file(src, self.opt.algorithm, self.opt.chunk_bytes, self.control, None,
                         bypass_cache=False)  # fmt: skip
        b, _ = hash_file(dst, self.opt.algorithm, self.opt.chunk_bytes, self.control, counted)
        self.tracker.copied += entry.size
        return a == b, a

    # -- copy, with retries
    def _copy_with_retries(
        self, entry: FileEntry, src: str, dst: str, result: FileResult
    ) -> FileResult:
        tmp = dst + PARTIAL_SUFFIX
        last_error = ""
        for attempt in range(1, self.opt.max_attempts + 1):
            result.attempts = attempt
            if self.hooks.on_attempt:
                self.hooks.on_attempt(entry.rel, attempt)
            before = (self.tracker.copied, self.tracker.verified)
            try:
                result.hash_hex, result.cache_bypassed = self._attempt(src, dst, tmp, entry)
            except CopyCancelledError:
                _quiet_unlink(tmp)
                raise
            except FileExistsError:
                _quiet_unlink(tmp)
                result.outcome = FileOutcome.CONFLICT
                result.error = "Another file appeared at the destination during the copy"
                return result
            except (OSError, RetryableCopyError, FatalCopyError) as exc:
                _quiet_unlink(tmp)
                if (reason := _fatal_reason(exc)) is not None:
                    result.error = reason
                    return result
                last_error = (
                    str(exc)
                    if isinstance(exc, RetryableCopyError)
                    else (f"{type(exc).__name__}: {exc}")
                )
                self.tracker.copied, self.tracker.verified = before  # discard the failed attempt
                log.warning("copy attempt %d/%d failed for %s: %s", attempt,
                            self.opt.max_attempts, entry.rel, last_error)  # fmt: skip
                if attempt < self.opt.max_attempts:
                    self.sleep(min(self.opt.backoff_base_s * 2 ** (attempt - 1), 30.0))
                    self.control.checkpoint()
            else:
                result.outcome = FileOutcome.COPIED
                return result
        result.error = f"Failed after {self.opt.max_attempts} attempts: {last_error}"
        return result

    def _attempt(self, src: str, dst: str, tmp: str, entry: FileEntry) -> tuple[str, bool]:
        before = os.stat(src)
        self.control.checkpoint()
        source_digest, total = self._stream_copy(src, tmp, entry)
        if total != before.st_size:
            raise SourceChangedError(
                f"Source size changed while copying ({before.st_size} -> {total})"
            )
        if self.hooks.after_write:
            self.hooks.after_write(Path(tmp))

        def counted(n: int) -> None:
            self.tracker.verified += n
            self.tracker.emit(entry.rel, "verify", total, entry.size)

        dest_digest, bypassed = hash_file(
            tmp, self.opt.algorithm, self.opt.chunk_bytes, self.control, counted
        )
        if dest_digest != source_digest:
            raise HashMismatchError(
                f"Checksum mismatch after copy (source {source_digest}, destination {dest_digest})"
            )
        after = os.stat(src)
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise SourceChangedError("Source file was modified while it was being copied")

        if self.hooks.before_finalise:
            self.hooks.before_finalise(Path(tmp))
        self.tracker.emit(entry.rel, "finalise", total, entry.size, force=True)
        os.utime(tmp, ns=(before.st_atime_ns, before.st_mtime_ns))  # keep source timestamps
        if entry.rel in self.opt.overwrite:
            os.replace(tmp, dst)  # the user approved replacing this exact file
        else:
            os.rename(tmp, dst)  # on Windows this refuses to overwrite: no silent clobbering
        return source_digest, bypassed

    def _stream_copy(self, src: str, tmp: str, entry: FileEntry) -> tuple[str, int]:
        hasher = new_hasher(self.opt.algorithm)
        buf = bytearray(self.opt.chunk_bytes)
        view = memoryview(buf)
        total = 0
        try:
            with open(src, "rb", buffering=0) as fin, open(tmp, "wb", buffering=0) as fout:
                while True:
                    self.control.checkpoint()
                    n = fin.readinto(buf)
                    if not n:
                        break
                    piece = view[:n]
                    hasher.update(piece)
                    _write_all(fout, piece)
                    piece.release()
                    total += n
                    self.tracker.copied += n
                    self.tracker.emit(entry.rel, "copy", total, entry.size)
                os.fsync(fout.fileno())  # the data must be on disk before we read it back
        finally:
            view.release()
        return hasher.hexdigest(), total


# --------------------------------------------------------------------------- whole tree
def copy_tree(
    plan: Plan,
    options: CopyOptions,
    control: Control | None = None,
    *,
    progress: ProgressCallback | None = None,
    hooks: Hooks | None = None,
    on_file_done: Callable[[FileResult], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> TreeResult:
    control = control or Control()
    started = time.monotonic()
    preflight(plan, options)
    for rel in plan.dirs:
        os.makedirs(extended(_real(plan.dst, rel)), exist_ok=True)

    tracker = _Tracker(plan.total_bytes, progress)
    copier = _FileCopier(plan, options, control, tracker, hooks or Hooks(), sleep)
    results: list[FileResult] = []
    cancelled = False
    try:
        for entry in plan.files:
            control.checkpoint()
            result = copier.copy(entry)
            results.append(result)
            if on_file_done:
                on_file_done(result)
            tracker.emit(entry.rel, "done", entry.size, entry.size, force=True)
    except CopyCancelledError:
        cancelled = True
    finally:
        cleanup_partials(plan.dst)

    tree = TreeResult(results, cancelled=cancelled, duration_s=time.monotonic() - started)
    if tree.ok and options.write_checksum_file:
        tree.manifest_path = write_manifest(plan, options.algorithm, results)
    return tree


def write_manifest(plan: Plan, algorithm: str, results: list[FileResult]) -> Path:
    """Write ``<folder>.checksums.<ext>`` in the format ``xxhsum -c`` / ``sha256sum -c`` read."""
    path = manifest_path(plan, algorithm)
    lines = [
        manifest_line(algorithm, r.hash_hex, r.rel)
        for r in sorted(results, key=lambda r: r.rel)
        if r.hash_hex
    ]
    tmp = extended(path) + PARTIAL_SUFFIX
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.writelines(lines)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, extended(path))  # replaces only our own manifest
    return path


def cleanup_partials(dst: Path) -> list[str]:
    """Delete leftover ``*.cspartial`` files under ``dst``. Nothing else is ever deleted."""
    removed: list[str] = []
    if not dst.is_dir():
        return removed
    for folder, _dirs, names in os.walk(extended(dst), followlinks=False):
        for name in names:
            if name.endswith(PARTIAL_SUFFIX):
                full = os.path.join(folder, name)
                _quiet_unlink(full)
                removed.append(full)
    return removed

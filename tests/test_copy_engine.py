"""Copy-engine tests: every failure mode must end with either a verified copy or a clear error,
and never a wrong file under its final name."""

from __future__ import annotations

import errno
import os
import subprocess
import threading
from pathlib import Path

import pytest

from contentshare.copy_engine import (
    MIB,
    Control,
    CopyOptions,
    FileOutcome,
    PreflightError,
    TrustedFile,
    build_plan,
    cleanup_partials,
    copy_tree,
    preflight,
)
from contentshare.copy_engine.engine import PARTIAL_SUFFIX, manifest_path
from contentshare.copy_engine.hashing import new_hasher
from contentshare.copy_engine.models import Hooks, Progress
from contentshare.copy_engine.winio import extended, hash_file


def no_sleep(_s: float) -> None:
    return None


OPTS = CopyOptions(chunk_bytes=MIB, backoff_base_s=0)


def make_tree(root: Path, files: dict[str, bytes]) -> Path:
    for rel, data in files.items():
        path = root.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def digest(path: Path, algorithm: str = "xxh3_128") -> str:
    h = new_hasher(algorithm)
    h.update(path.read_bytes())
    return h.hexdigest()


@pytest.fixture
def src(tmp_path: Path) -> Path:
    return make_tree(
        tmp_path / "src",
        {
            "a.bin": os.urandom(3 * MIB + 123),
            "sub/b.txt": b"hello world",
            "sub/deep/c.dat": os.urandom(50_000),
            "empty.dat": b"",
        },
    )


@pytest.fixture
def dst(tmp_path: Path) -> Path:
    return tmp_path / "dest" / "Program X"


def run(src: Path, dst: Path, options: CopyOptions = OPTS, **kw: object):  # type: ignore[no-untyped-def]
    return copy_tree(build_plan(src, dst), options, sleep=no_sleep, **kw)  # type: ignore[arg-type]


def partials(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.name.endswith(PARTIAL_SUFFIX)]


# ------------------------------------------------------------------ happy path
@pytest.mark.parametrize("algorithm", ["xxh3_128", "blake3", "sha256"])
def test_copies_tree_exactly(src: Path, dst: Path, algorithm: str) -> None:
    result = run(src, dst, CopyOptions(algorithm=algorithm, chunk_bytes=MIB))
    assert result.ok
    for f in src.rglob("*"):
        if f.is_file():
            copy = dst / f.relative_to(src)
            assert copy.read_bytes() == f.read_bytes()
            assert copy.stat().st_mtime_ns == f.stat().st_mtime_ns  # timestamps preserved
    assert not partials(dst)
    assert {r.outcome for r in result.files} == {FileOutcome.COPIED}
    assert all(r.cache_bypassed for r in result.files)  # read-back skipped the OS cache


def test_manifest_is_standard_and_correct(src: Path, dst: Path) -> None:
    result = run(src, dst)
    assert result.manifest_path == manifest_path(build_plan(src, dst), "xxh3_128")
    lines = result.manifest_path.read_text(encoding="utf-8").splitlines()  # type: ignore[union-attr]
    assert len(lines) == 4
    for line in lines:
        digest_hex, _, rel = line.partition("  ")
        assert digest_hex == digest(dst / rel)
    sha = run(src, dst / "x", CopyOptions(algorithm="sha256", chunk_bytes=MIB))
    assert sha.manifest_path.suffix == ".sha256sum"  # type: ignore[union-attr]
    assert " *" in sha.manifest_path.read_text(encoding="utf-8")  # type: ignore[union-attr]


def test_single_file_and_empty_dirs(tmp_path: Path, dst: Path) -> None:
    f = make_tree(tmp_path / "one", {"movie.mxf": b"x" * 1000}) / "movie.mxf"
    assert run(f, dst).ok
    assert (dst / "movie.mxf").read_bytes() == b"x" * 1000

    folder = tmp_path / "withempty"
    (folder / "nothing" / "here").mkdir(parents=True)
    assert run(folder, tmp_path / "out2").ok
    assert (tmp_path / "out2" / "nothing" / "here").is_dir()


def test_zero_byte_file(tmp_path: Path, dst: Path) -> None:
    s = make_tree(tmp_path / "z", {"zero.bin": b""})
    assert run(s, dst).ok
    assert (dst / "zero.bin").stat().st_size == 0


def test_unicode_and_emoji_names(tmp_path: Path, dst: Path) -> None:
    s = make_tree(tmp_path / "u", {"कार्यक्रम/ध्यान 🎬.mp4": b"abc", "naïve café.txt": b"d"})
    assert run(s, dst).ok
    assert (dst / "कार्यक्रम" / "ध्यान 🎬.mp4").read_bytes() == b"abc"


def test_long_paths_over_260_chars(tmp_path: Path) -> None:
    deep = "/".join(["d" * 50] * 7)  # ~360 characters once rooted
    s = tmp_path / "s"
    ext_file = Path(extended(s.joinpath(*deep.split("/"), "f.txt")))
    os.makedirs(ext_file.parent, exist_ok=True)
    ext_file.write_bytes(b"long")
    out = tmp_path / "o"
    assert len(str(out.joinpath(*deep.split("/"), "f.txt"))) > 260
    result = run(s, out)
    assert result.ok
    assert Path(extended(out.joinpath(*deep.split("/"), "f.txt"))).read_bytes() == b"long"


def test_deep_and_wide_tree(tmp_path: Path, dst: Path) -> None:
    s = make_tree(
        tmp_path / "w", {f"d{i % 7}/e{i % 3}/f{i}.txt": str(i).encode() for i in range(300)}
    )
    result = run(s, dst)
    assert result.ok
    assert len(result.files) == 300


def test_tiny_chunks_exercise_the_loop(src: Path, dst: Path) -> None:
    assert run(src, dst, CopyOptions(chunk_bytes=MIB, backoff_base_s=0)).ok


# ------------------------------------------------------------------ corruption & retries
def test_bit_flip_in_destination_is_detected_and_recopied(src: Path, dst: Path) -> None:
    flips = {"count": 0}

    def corrupt_first_time(tmp: Path) -> None:
        if tmp.name.startswith("a.bin") and flips["count"] == 0:
            flips["count"] += 1
            with tmp.open("r+b") as handle:
                handle.seek(1_000_000)
                byte = handle.read(1)
                handle.seek(1_000_000)
                handle.write(bytes([byte[0] ^ 0x01]))

    result = run(src, dst, hooks=Hooks(after_write=corrupt_first_time))
    assert result.ok
    a = next(r for r in result.files if r.rel == "a.bin")
    assert a.attempts == 2
    assert (dst / "a.bin").read_bytes() == (src / "a.bin").read_bytes()


def test_persistent_corruption_fails_and_leaves_no_final_file(src: Path, dst: Path) -> None:
    def always_corrupt(tmp: Path) -> None:
        if tmp.name.startswith("sub") or tmp.name.startswith("b.txt"):
            tmp.write_bytes(b"garbage!!!!")

    result = run(src, dst, hooks=Hooks(after_write=always_corrupt))
    assert not result.ok
    bad = result.failures[0]
    assert bad.rel == "sub/b.txt"
    assert bad.attempts == 3
    assert "mismatch" in (bad.error or "").lower()
    assert not (dst / "sub" / "b.txt").exists()
    assert not partials(dst)
    assert result.manifest_path is None  # no manifest for an incomplete job


def test_network_error_is_retried(src: Path, dst: Path) -> None:
    seen: list[int] = []

    def flaky(rel: str, attempt: int) -> None:
        seen.append(attempt)
        if rel == "sub/b.txt" and attempt < 3:
            raise OSError(errno.ECONNRESET, "network name no longer available")

    # raised from on_attempt, which runs inside the retry loop's try block? No: it runs before it.
    # so simulate the drop where reads happen:
    def drop_after_write(tmp: Path) -> None:
        if tmp.name.startswith("b.txt") and len(seen) < 10 and flaky_state["n"] < 2:
            flaky_state["n"] += 1
            raise OSError(errno.ECONNRESET, "network name no longer available")

    flaky_state = {"n": 0}
    result = run(src, dst, hooks=Hooks(after_write=drop_after_write))
    assert result.ok
    assert next(r for r in result.files if r.rel == "sub/b.txt").attempts == 3


def test_source_modified_during_copy_is_detected(src: Path, dst: Path) -> None:
    state = {"done": False}

    def touch_source(tmp: Path) -> None:
        if tmp.name.startswith("b.txt") and not state["done"]:
            state["done"] = True
            (src / "sub" / "b.txt").write_bytes(b"changed while copying!")

    result = run(src, dst, hooks=Hooks(after_write=touch_source))
    assert result.ok
    b = next(r for r in result.files if r.rel == "sub/b.txt")
    assert b.attempts == 2  # first attempt rejected, second copied the new content
    assert (dst / "sub" / "b.txt").read_bytes() == b"changed while copying!"


def test_source_that_never_stops_changing_fails(src: Path, dst: Path) -> None:
    counter = {"n": 0}

    def keep_touching(tmp: Path) -> None:
        if tmp.name.startswith("b.txt"):
            counter["n"] += 1
            (src / "sub" / "b.txt").write_bytes(b"x" * (counter["n"] + 20))

    result = run(src, dst, hooks=Hooks(after_write=keep_touching))
    assert not result.ok
    assert not (dst / "sub" / "b.txt").exists()
    assert "modified" in (result.failures[0].error or "") or "changed" in (
        result.failures[0].error or ""
    )


class SimulatedCrash(BaseException):
    pass


def test_crash_mid_copy_never_leaves_a_bad_final_file_and_resumes(src: Path, dst: Path) -> None:
    def die(tmp: Path) -> None:
        if tmp.name.startswith("b.txt"):
            raise SimulatedCrash

    with pytest.raises(SimulatedCrash):
        run(src, dst, hooks=Hooks(after_write=die))
    assert not (dst / "sub" / "b.txt").exists()  # the final name was never written
    cleanup_partials(dst)  # what the job runner does on the next start
    assert not partials(dst)

    result = run(src, dst)  # "restart"
    assert result.ok
    by_rel = {r.rel: r.outcome for r in result.files}
    assert by_rel["a.bin"] == FileOutcome.SKIPPED_IDENTICAL  # finished before the crash: not redone
    assert by_rel["sub/b.txt"] == FileOutcome.COPIED
    assert (dst / "sub" / "b.txt").read_bytes() == b"hello world"


def test_disk_full_fails_fast_without_retrying(src: Path, dst: Path) -> None:
    attempts: list[int] = []

    def full(tmp: Path) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    result = run(
        src, dst, hooks=Hooks(after_write=full, on_attempt=lambda _r, n: attempts.append(n))
    )
    assert not result.ok
    assert "disk is full" in (result.failures[0].error or "")
    assert max(attempts) == 1  # no pointless retries
    assert not partials(dst)


# ------------------------------------------------------------------ what's already there
def test_identical_existing_files_are_skipped(src: Path, dst: Path) -> None:
    assert run(src, dst).ok
    again = run(src, dst)
    assert again.ok
    assert {r.outcome for r in again.files} == {FileOutcome.SKIPPED_IDENTICAL}
    assert partials(dst) == []


def test_different_existing_file_is_never_overwritten_silently(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True)
    (dst / "sub").mkdir()
    (dst / "sub" / "b.txt").write_bytes(b"someone else's content")  # same size would be 11; differs
    result = run(src, dst)
    assert not result.ok
    assert [r.rel for r in result.failures] == ["sub/b.txt"]
    assert result.failures[0].outcome == FileOutcome.CONFLICT
    assert (dst / "sub" / "b.txt").read_bytes() == b"someone else's content"
    assert (dst / "a.bin").read_bytes() == (src / "a.bin").read_bytes()  # others still delivered


def test_same_size_different_content_is_a_conflict(src: Path, dst: Path) -> None:
    (dst / "sub").mkdir(parents=True)
    (dst / "sub" / "b.txt").write_bytes(b"HELLO WORLD")  # same 11 bytes, different content
    result = run(src, dst)
    assert result.failures[0].outcome == FileOutcome.CONFLICT


def test_overwrite_only_with_explicit_consent(src: Path, dst: Path) -> None:
    (dst / "sub").mkdir(parents=True)
    (dst / "sub" / "b.txt").write_bytes(b"old")
    opts = CopyOptions(chunk_bytes=MIB, backoff_base_s=0, overwrite=frozenset({"sub/b.txt"}))
    result = run(src, dst, opts)
    assert result.ok
    assert (dst / "sub" / "b.txt").read_bytes() == b"hello world"


def test_file_appearing_during_copy_is_never_clobbered(src: Path, dst: Path) -> None:
    def someone_else_writes(tmp: Path) -> None:
        if tmp.name.startswith("b.txt"):
            final = tmp.with_name("b.txt")
            final.write_bytes(b"created by someone else mid-copy")

    result = run(src, dst, hooks=Hooks(before_finalise=someone_else_writes))
    assert [r.rel for r in result.failures] == ["sub/b.txt"]
    assert result.failures[0].outcome == FileOutcome.CONFLICT
    assert (dst / "sub" / "b.txt").read_bytes() == b"created by someone else mid-copy"
    assert not partials(dst)


def test_folder_in_the_way_is_a_conflict(src: Path, dst: Path) -> None:
    (dst / "a.bin").mkdir(parents=True)
    result = run(src, dst)
    assert result.failures[0].rel == "a.bin"
    assert result.failures[0].outcome == FileOutcome.CONFLICT


def test_trusted_files_are_skipped_without_reading(src: Path, dst: Path) -> None:
    first = run(src, dst)
    trusted = {r.rel: TrustedFile(r.size, r.mtime_ns, r.hash_hex or "") for r in first.files}
    reads: list[str] = []
    again = run(
        src,
        dst,
        CopyOptions(chunk_bytes=MIB, backoff_base_s=0, trusted=trusted),
        hooks=Hooks(on_attempt=lambda rel, _n: reads.append(rel)),
    )
    assert {r.outcome for r in again.files} == {FileOutcome.SKIPPED_TRUSTED}
    assert reads == []

    (dst / "sub" / "b.txt").write_bytes(b"tampered!!!")  # same size, new mtime -> not trusted
    after = run(src, dst, CopyOptions(chunk_bytes=MIB, backoff_base_s=0, trusted=trusted))
    assert next(r for r in after.files if r.rel == "sub/b.txt").outcome == FileOutcome.CONFLICT


# ------------------------------------------------------------------ pre-flight
def test_preflight_missing_source(tmp_path: Path, dst: Path) -> None:
    with pytest.raises(PreflightError, match="Source not found"):
        preflight(build_plan(tmp_path / "nope", dst), OPTS)


def test_preflight_reserved_and_trailing_dot_names(tmp_path: Path, dst: Path) -> None:
    s = tmp_path / "s"
    s.mkdir()
    for name in ("CON.txt", "bad."):
        # Only creatable with a hand-built \\?\ path: normal path handling strips "bad." to "bad".
        Path("\\\\?\\" + str(s) + "\\" + name).write_bytes(b"x")
    plan = build_plan(s, dst)
    assert len(plan.problems) == 2
    with pytest.raises(PreflightError, match="not valid on Windows"):
        preflight(plan, OPTS)
    assert not dst.exists()  # nothing was created


def test_preflight_not_enough_space(src: Path, dst: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("contentshare.copy_engine.engine.free_space", lambda _p: 1000)
    with pytest.raises(PreflightError, match="Not enough free space"):
        preflight(build_plan(src, dst), OPTS)


def test_dry_run_preflight_creates_nothing(src: Path, dst: Path) -> None:
    preflight(build_plan(src, dst), OPTS, create_dirs=False)
    assert not dst.exists()


def test_case_collisions_are_blocked(tmp_path: Path, dst: Path) -> None:
    s = tmp_path / "s"
    s.mkdir()
    (s / "File.txt").write_bytes(b"1")
    plan = build_plan(s, dst)
    plan.files.append(
        plan.files[0].__class__("file.txt", 1, 0)
    )  # what a case-sensitive NAS could show
    plan.problems.append("Names differ only by case: File.txt and file.txt")
    with pytest.raises(PreflightError, match="only by case"):
        preflight(plan, OPTS)


def test_source_file_named_like_manifest_is_blocked(tmp_path: Path) -> None:
    dst = tmp_path / "Out"
    s = make_tree(tmp_path / "s", {"Out.checksums.xxhash": b"evil", "a.txt": b"a"})
    with pytest.raises(PreflightError, match="named like the checksum file"):
        preflight(build_plan(s, dst), OPTS)


def test_junctions_are_not_followed(tmp_path: Path, dst: Path) -> None:
    secret = make_tree(tmp_path / "secret", {"passwords.txt": b"hunter2"})
    s = make_tree(tmp_path / "s", {"ok.txt": b"fine"})
    made = subprocess.run(  # noqa: S603
        ["cmd", "/c", "mklink", "/J", str(s / "link"), str(secret)],  # noqa: S607
        capture_output=True,
        check=False,
    )
    if made.returncode != 0:
        pytest.skip("cannot create a junction here")
    plan = build_plan(s, dst)
    assert [f.rel for f in plan.files] == ["ok.txt"]
    assert any("link" in w for w in plan.warnings)
    assert run(s, dst).ok
    assert not (dst / "link").exists()


def test_read_only_destination_fails_cleanly(tmp_path: Path, src: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    user = os.environ.get("USERNAME", "")
    deny = subprocess.run(  # noqa: S603
        ["icacls", str(locked), "/deny", f"{user}:(OI)(CI)(WD,AD)"],  # noqa: S607
        capture_output=True,
        check=False,
    )
    try:
        probe = locked / "probe"
        try:
            probe.write_bytes(b"x")
            pytest.skip("could not make a read-only folder with icacls")
        except OSError:
            pass
        if deny.returncode != 0:
            pytest.skip("icacls unavailable")
        with pytest.raises(PreflightError, match="not writable"):
            copy_tree(build_plan(src, locked / "out"), OPTS, sleep=no_sleep)
    finally:
        subprocess.run(  # noqa: S603
            ["icacls", str(locked), "/remove:d", user],  # noqa: S607
            capture_output=True,
            check=False,
        )


def test_locked_source_file_fails_after_retries(tmp_path: Path, dst: Path) -> None:
    import ctypes
    from ctypes import wintypes

    s = make_tree(tmp_path / "s", {"inuse.bin": b"data" * 1000, "free.bin": b"ok"})
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]  # fmt: skip
    k32.CreateFileW.restype = ctypes.c_void_p
    handle = k32.CreateFileW(str(s / "inuse.bin"), 0x80000000 | 0x40000000, 0, None, 3, 0, None)
    assert handle not in (None, ctypes.c_void_p(-1).value)
    try:
        result = run(s, dst)
    finally:
        k32.CloseHandle(ctypes.c_void_p(handle))
    assert not result.ok
    assert result.failures[0].rel == "inuse.bin"
    assert result.failures[0].attempts == 3
    assert (dst / "free.bin").read_bytes() == b"ok"  # one locked file doesn't block the rest
    assert not (dst / "inuse.bin").exists()


# ------------------------------------------------------------------ control & progress
def test_progress_reports_stages_and_reaches_100(src: Path, dst: Path) -> None:
    events: list[Progress] = []
    run(src, dst, progress=events.append)
    assert {e.stage for e in events} >= {"copy", "verify", "finalise", "done"}
    assert events[-1].fraction == pytest.approx(1.0)
    assert events[-1].total_bytes == sum(f.stat().st_size for f in src.rglob("*") if f.is_file())
    fractions = [e.fraction for e in events]
    assert fractions == sorted(fractions)  # never goes backwards


def test_cancel_stops_cleanly_and_removes_partials(src: Path, dst: Path) -> None:
    control = Control()

    def cancel_on_second_file(rel: str, _attempt: int) -> None:
        if rel == "empty.dat":
            control.cancel()

    result = copy_tree(
        build_plan(src, dst),
        OPTS,
        control,
        sleep=no_sleep,
        hooks=Hooks(on_attempt=cancel_on_second_file),
    )
    assert result.cancelled
    assert not result.ok
    assert not partials(dst)
    assert result.manifest_path is None


def test_pause_blocks_until_resumed(src: Path, dst: Path) -> None:
    control = Control()
    control.pause()
    finished = threading.Event()
    outcome: list[bool] = []

    def worker() -> None:
        outcome.append(copy_tree(build_plan(src, dst), OPTS, control, sleep=no_sleep).ok)
        finished.set()

    thread = threading.Thread(target=worker)
    thread.start()
    assert not finished.wait(0.7)  # paused: nothing happens
    control.resume()
    assert finished.wait(10)
    thread.join()
    assert outcome == [True]


def test_cancel_wakes_a_paused_job(src: Path, dst: Path) -> None:
    control = Control()
    control.pause()
    done: list[bool] = []
    thread = threading.Thread(
        target=lambda: done.append(
            copy_tree(build_plan(src, dst), OPTS, control, sleep=no_sleep).cancelled
        )
    )
    thread.start()
    control.cancel()
    thread.join(10)
    assert done == [True]


def test_progress_callback_errors_do_not_break_the_copy(src: Path, dst: Path) -> None:
    def boom(_p: Progress) -> None:
        raise RuntimeError("UI exploded")

    assert run(src, dst, progress=boom).ok


# ------------------------------------------------------------------ cleanup & hashing
def test_cleanup_only_removes_partial_files(tmp_path: Path) -> None:
    root = make_tree(
        tmp_path / "d",
        {"keep.txt": b"1", "a.mp4.cspartial": b"2", "x/b.cspartial": b"3", "x/keep.cs": b"4"},
    )
    removed = cleanup_partials(root)
    assert len(removed) == 2
    assert sorted(p.name for p in root.rglob("*") if p.is_file()) == ["keep.cs", "keep.txt"]
    assert cleanup_partials(tmp_path / "missing") == []


def test_unbuffered_and_buffered_hashes_agree(tmp_path: Path) -> None:
    f = tmp_path / "f.bin"
    f.write_bytes(os.urandom(2 * MIB + 17))  # not a multiple of the sector size
    control = Control()
    fast, bypassed = hash_file(str(f), "xxh3_128", MIB, control)
    slow, not_bypassed = hash_file(str(f), "xxh3_128", MIB, control, bypass_cache=False)
    assert bypassed
    assert not not_bypassed
    assert fast == slow == digest(f)


def test_hash_of_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        hash_file(str(tmp_path / "nope"), "xxh3_128", MIB, Control())


# ------------------------------------------------------------------ big files
@pytest.mark.slow
def test_file_larger_than_4_gib(tmp_path: Path, dst: Path) -> None:
    """Catches 32-bit size/offset bugs. Uses a sparse source so creating it is instant."""
    s = tmp_path / "s"
    s.mkdir()
    big = s / "big.mxf"
    big.write_bytes(b"")
    subprocess.run(["fsutil", "sparse", "setflag", str(big)], check=True, capture_output=True)  # noqa: S603,S607
    size = 4 * 1024**3 + 12345
    with big.open("r+b") as handle:
        handle.seek(size - 4)
        handle.write(b"TAIL")
    opts = CopyOptions(chunk_bytes=16 * MIB, backoff_base_s=0)
    result = run(s, dst, opts)
    assert result.ok
    assert (dst / "big.mxf").stat().st_size == size
    with (dst / "big.mxf").open("rb") as handle:
        handle.seek(size - 4)
        assert handle.read() == b"TAIL"

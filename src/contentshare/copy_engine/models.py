"""Value types shared by the copy engine and the job layer."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

MIB = 1024 * 1024


class FileOutcome(StrEnum):
    COPIED = "COPIED"
    SKIPPED_IDENTICAL = "SKIPPED_IDENTICAL"  # already at the destination, hashes match
    SKIPPED_TRUSTED = "SKIPPED_TRUSTED"  # verified by an earlier run, size+mtime unchanged
    CONFLICT = "CONFLICT"  # different file already there; never overwritten silently
    FAILED = "FAILED"


class CopyError(Exception):
    """Base class for engine errors."""


class FatalCopyError(CopyError):
    """Retrying cannot help (for example the destination disk is full)."""


class RetryableCopyError(CopyError):
    """A transient or detectable failure: network drop, hash mismatch, source changed."""


class HashMismatchError(RetryableCopyError):
    pass


class SourceChangedError(RetryableCopyError):
    pass


class CopyCancelledError(CopyError):
    pass


class PreflightError(CopyError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class FileEntry:
    rel: str  # always "/" separated, relative to the plan root
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class TrustedFile:
    """A file a previous run verified. Skipped without re-reading if the destination still
    has the same size and modification time."""

    size: int
    mtime_ns: int
    hash_hex: str


@dataclass
class Plan:
    src: Path
    dst: Path
    files: list[FileEntry] = field(default_factory=list)
    dirs: list[str] = field(default_factory=list)  # relative, includes empty directories
    warnings: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # block the copy

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)


@dataclass(frozen=True)
class CopyOptions:
    algorithm: str = "xxh3_128"
    write_checksum_file: bool = True  # leave <folder>.checksums.<ext> beside the copied files
    chunk_bytes: int = 8 * MIB
    max_attempts: int = 3
    backoff_base_s: float = 1.0
    free_space_margin_pct: int = 5
    overwrite: frozenset[str] = frozenset()  # rel paths the user agreed to replace
    trusted: Mapping[str, TrustedFile] = field(default_factory=dict)


@dataclass
class FileResult:
    rel: str
    outcome: FileOutcome
    size: int
    mtime_ns: int = 0
    hash_hex: str | None = None
    attempts: int = 0
    error: str | None = None
    cache_bypassed: bool = False


@dataclass
class TreeResult:
    files: list[FileResult]
    cancelled: bool = False
    manifest_path: Path | None = None
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        bad = {FileOutcome.FAILED, FileOutcome.CONFLICT}
        return not self.cancelled and all(f.outcome not in bad for f in self.files)

    @property
    def failures(self) -> list[FileResult]:
        return [f for f in self.files if f.outcome in {FileOutcome.FAILED, FileOutcome.CONFLICT}]


@dataclass(frozen=True)
class Progress:
    rel: str
    stage: str  # "copy" | "verify" | "finalise" | "done"
    file_done: int
    file_size: int
    copied_bytes: int
    verified_bytes: int
    total_bytes: int
    speed_bps: float
    eta_s: float | None

    @property
    def fraction(self) -> float:
        """Overall progress: copying and verifying each count for half."""
        if self.total_bytes == 0:
            return 1.0
        return min(1.0, (self.copied_bytes + self.verified_bytes) / (2 * self.total_bytes))


ProgressCallback = Callable[[Progress], None]


class Control:
    """Pause / resume / cancel, checked between chunks. Safe to call from another thread."""

    def __init__(self) -> None:
        self._cancel = threading.Event()
        self._running = threading.Event()
        self._running.set()

    def cancel(self) -> None:
        self._cancel.set()
        self._running.set()  # wake a paused worker so it can exit

    def pause(self) -> None:
        self._running.clear()

    def resume(self) -> None:
        self._running.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def paused(self) -> bool:
        return not self._running.is_set()

    def checkpoint(self) -> None:
        while not self._running.wait(timeout=0.25):
            if self._cancel.is_set():
                break
        if self._cancel.is_set():
            raise CopyCancelledError("Cancelled")


@dataclass
class Hooks:
    """Test seams for fault injection. Production code leaves these unset."""

    after_write: Callable[[Path], None] | None = None  # temp file written, before read-back
    before_finalise: Callable[[Path], None] | None = None  # verified, before the rename
    on_attempt: Callable[[str, int], None] | None = None  # (rel, attempt number)

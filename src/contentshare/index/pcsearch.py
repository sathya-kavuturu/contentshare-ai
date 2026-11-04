"""Looking for a program on this whole PC, when it is not in a department's own folders.

The storage index only knows the folders listed in Settings. This is the wider, slower search a
department can switch on: it walks the PC's own fixed drives (never network drives, never
removable media), reading only folder and file *names*, and stops after a time limit or when
asked. It never reads file contents and never changes anything.

Windows and Program Files, the recycle bin, other users' application data, and folders that are
only shortcuts or links are skipped, as are the department destination folders (copies already
made there are not the original). What it finds is only ever a suggestion for the person to
pick: nothing found here is marked Ready by itself.
"""

from __future__ import annotations

import ctypes
import fnmatch
import logging
import os
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import PureWindowsPath

from contentshare.config.models import DEFAULT_EXCLUDES, inside_system_folder, paths_overlap
from contentshare.copy_engine.winio import extended, is_reparse_or_link
from contentshare.index.store import IndexedItem, query_tokens
from contentshare.parsing.text import split_camel

log = logging.getLogger(__name__)

DRIVE_FIXED = 3  # GetDriveTypeW: a local hard disk or SSD
DEFAULT_SECONDS = 120.0
MAX_HITS = 500
_PROGRESS_EVERY = 1500
_TOKEN = re.compile(r"[^\W_]+")

# Folders that never hold what is asked for, and can hold millions of entries.
SKIP_NAMES = frozenset(
    name.casefold()
    for name in (
        "$Recycle.Bin", "System Volume Information", "Windows", "Windows.old", "$WinREAgent",
        "Recovery", "ProgramData", "Program Files", "Program Files (x86)", "AppData",
        "node_modules", ".git", ".venv", "venv", "__pycache__", "site-packages",
    )
)  # fmt: skip


def fixed_drives() -> list[str]:
    """``["C:\\\\", "D:\\\\"]``: the PC's own disks, asked of Windows."""
    kernel = ctypes.windll.kernel32  # type: ignore[attr-defined,unused-ignore]
    mask = int(kernel.GetLogicalDrives())
    drives = []
    for i in range(26):
        if mask & (1 << i):
            root = f"{chr(ord('A') + i)}:\\"
            if kernel.GetDriveTypeW(root) == DRIVE_FIXED:
                drives.append(root)
    return drives


@dataclass
class PcSearchResult:
    items: list[IndexedItem]
    complete: bool  # False: it stopped (time limit or cancelled) before looking everywhere
    folders_looked_at: int


def _matcher(program: str) -> Callable[[str], bool] | None:
    """A cheap test on a name. The scorer judges the real match afterwards; this only has to
    keep the list short. Every word of one or two, else two thirds of them, must start a word
    of the name."""
    wanted = query_tokens(program)
    if not wanted:
        return None
    need = len(wanted) if len(wanted) <= 2 else -(-2 * len(wanted) // 3)

    def matches(name: str) -> bool:
        words = _TOKEN.findall(split_camel(name).casefold())
        hit = sum(1 for token in wanted if any(w.startswith(token) for w in words))
        return hit >= need

    return matches


def _children(folder: str) -> list[tuple[str, bool]]:
    """``(name, is_folder)`` of what is in ``folder``, leaving out shortcuts and links and
    anything that cannot be read (no permission, or it vanished: not an error for a search)."""
    out: list[tuple[str, bool]] = []
    try:
        with os.scandir(extended(folder)) as scanner:
            for entry in scanner:
                try:
                    if not is_reparse_or_link(entry):
                        out.append((entry.name, entry.is_dir(follow_symlinks=False)))
                except OSError:
                    continue
    except OSError:
        return []
    return out


def search_pc(
    program: str,
    *,
    drives: Iterable[str] | None = None,
    skip: Iterable[str] = (),
    should_stop: Callable[[], bool] | None = None,
    progress: Callable[[int, str], None] | None = None,
    seconds: float = DEFAULT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> PcSearchResult:
    """Folders and files whose name looks like ``program`` on this PC's own drives. ``skip`` are
    folders not to enter (the departments' destination folders)."""
    matches = _matcher(program)
    if matches is None:
        return PcSearchResult([], True, 0)
    skipped = [s for s in skip if s]
    patterns = [p.casefold() for p in DEFAULT_EXCLUDES]
    deadline = clock() + seconds
    items: list[IndexedItem] = []
    looked_at = 0

    def excluded(name: str, path: str) -> bool:
        folded = name.casefold()
        if folded in SKIP_NAMES or any(fnmatch.fnmatchcase(folded, p) for p in patterns):
            return True
        return bool(inside_system_folder(path)) or any(paths_overlap(path, s) for s in skipped)

    for drive in drives if drives is not None else fixed_drives():
        stack = [(drive, "", 0)]  # (full path, path relative to the drive, depth)
        while stack:
            if (should_stop and should_stop()) or clock() > deadline:
                return PcSearchResult(items, False, looked_at)
            folder, rel, depth = stack.pop()
            looked_at += 1
            if progress and looked_at % _PROGRESS_EVERY == 0:
                progress(looked_at, folder)
            for name, is_dir in _children(folder):
                path = str(PureWindowsPath(folder, name))
                if excluded(name, path):
                    continue
                child = f"{rel}/{name}" if rel else name
                if len(items) < MAX_HITS and matches(name):
                    kind = "dir" if is_dir else "file"
                    items.append(IndexedItem(0, drive, child, name, kind, 0, 0, 0, depth))
                if is_dir:
                    stack.append((path, child, depth + 1))
    return PcSearchResult(items, True, looked_at)

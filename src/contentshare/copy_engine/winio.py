"""Windows file I/O helpers: long paths and cache-bypassing reads.

``extended()`` adds the ``\\\\?\\`` prefix so paths over 260 characters work.

``hash_file()`` reads a file back with ``FILE_FLAG_NO_BUFFERING`` where the file system allows
it, so that verification reads what is really stored rather than what the OS cache remembers
from the write. Unbuffered reads need page-aligned buffers; an anonymous ``mmap`` is always
page-aligned, which avoids needing ``VirtualAlloc``.
"""

from __future__ import annotations

import contextlib
import ctypes
import mmap
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

from contentshare.copy_engine.hashing import new_hasher
from contentshare.copy_engine.models import Control
from contentshare.copy_engine.readahead import depth_for, read_ahead

_ERROR_INVALID_PARAMETER = 87
_ERROR_NOT_SUPPORTED = 50


def extended(path: str | os.PathLike[str]) -> str:
    """Absolute path in ``\\\\?\\`` form (UNC paths become ``\\\\?\\UNC\\server\\share\\...``)."""
    # abspath, not resolve(): we must not follow links or rewrite mapped drives here
    text = os.path.abspath(os.fspath(path))
    if text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def _hash_buffered(
    path: str, algorithm: str, chunk: int, control: Control, on_bytes: Callable[[int], None] | None
) -> str:
    hasher = new_hasher(algorithm)
    views = [memoryview(bytearray(chunk)) for _ in range(depth_for(chunk))]
    try:
        with open(path, "rb", buffering=0) as handle:
            chunks = read_ahead(lambda i: handle.readinto(views[i]) or 0, views, hasher.update)
            with contextlib.closing(chunks):
                for piece in chunks:
                    control.checkpoint()
                    if on_bytes:
                        on_bytes(len(piece))
    finally:
        for view in views:
            view.release()
    return hasher.hexdigest()


if sys.platform == "win32":
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    _k32.CreateFileW.restype = ctypes.c_void_p
    _k32.ReadFile.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    _k32.ReadFile.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]
    _k32.CloseHandle.restype = wintypes.BOOL

    _INVALID_HANDLE = ctypes.c_void_p(-1).value
    _GENERIC_READ = 0x80000000
    _SHARE_ALL = 0x7  # read | write | delete: never block someone else, never be blocked by readers
    _OPEN_EXISTING = 3
    _FLAG_NO_BUFFERING = 0x20000000
    _FLAG_SEQUENTIAL = 0x08000000

    def _open_unbuffered(path: str) -> int | None:
        handle = _k32.CreateFileW(
            path, _GENERIC_READ, _SHARE_ALL, None, _OPEN_EXISTING,
            _FLAG_NO_BUFFERING | _FLAG_SEQUENTIAL, None,
        )  # fmt: skip
        if handle == _INVALID_HANDLE or handle is None:
            code = ctypes.get_last_error()
            if code in {_ERROR_INVALID_PARAMETER, _ERROR_NOT_SUPPORTED}:
                return None  # this file system cannot do unbuffered reads: fall back
            raise ctypes.WinError(code)
        return int(handle)

    def hash_file(
        path: str,
        algorithm: str,
        chunk: int,
        control: Control,
        on_bytes: Callable[[int], None] | None = None,
        *,
        bypass_cache: bool = True,
    ) -> tuple[str, bool]:
        """Return ``(hex digest, whether the OS cache was bypassed)``."""
        handle = _open_unbuffered(path) if bypass_cache else None
        if handle is None:
            return _hash_buffered(path, algorithm, chunk, control, on_bytes), False

        hasher = new_hasher(algorithm)
        # page-aligned buffers (anonymous mmaps), as unbuffered reads require
        regions = [mmap.mmap(-1, chunk) for _ in range(depth_for(chunk))]
        anchors = [ctypes.c_char.from_buffer(r) for r in regions]
        addresses = [ctypes.addressof(a) for a in anchors]
        views = [memoryview(r) for r in regions]
        read = wintypes.DWORD(0)  # only the reader thread uses it

        def read_into(i: int) -> int:
            if not _k32.ReadFile(handle, addresses[i], chunk, ctypes.byref(read), None):
                raise ctypes.WinError(ctypes.get_last_error())
            return int(read.value)

        try:
            chunks = read_ahead(read_into, views, hasher.update)
            with contextlib.closing(chunks):
                for piece in chunks:
                    control.checkpoint()
                    if on_bytes:
                        on_bytes(len(piece))
        finally:
            for view in views:
                view.release()
            anchors.clear()
            for region in regions:
                region.close()
            _k32.CloseHandle(handle)
        return hasher.hexdigest(), True

else:  # pragma: no cover - the app targets Windows; this keeps imports working elsewhere

    def hash_file(
        path: str,
        algorithm: str,
        chunk: int,
        control: Control,
        on_bytes: Callable[[int], None] | None = None,
        *,
        bypass_cache: bool = True,
    ) -> tuple[str, bool]:
        return _hash_buffered(path, algorithm, chunk, control, on_bytes), False


def is_reparse_or_link(entry: os.DirEntry[str]) -> bool:
    """Symlinks and junctions are never followed when scanning a source tree."""
    return entry.is_symlink() or entry.is_junction()


def free_space(path: Path) -> int:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free

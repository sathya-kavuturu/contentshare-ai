"""Path safety.

Every path the app reads from or writes to must pass through here. Text that came from an
email is only ever used after :func:`sanitize_component`, and every final path is checked
with :func:`resolve_within` against the configured root (after following symlinks/junctions).
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Iterable
from pathlib import Path, PureWindowsPath

__all__ = [
    "PathSafetyError",
    "check_component",
    "ensure_absolute_windows_path",
    "join_within",
    "resolve_within",
    "resolve_within_any",
    "sanitize_component",
]

RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{dev}{n}" for dev in ("COM", "LPT") for n in [*"123456789", "¹", "²", "³"]}
)
_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SEPARATORS = re.compile(r"[\\/]")
_UNC_SHARE = re.compile(r"^\\\\[^\\/]+\\[^\\/]+$")
MAX_COMPONENT_LEN = 120


class PathSafetyError(ValueError):
    """A path is unsafe to read from or write to."""


def _is_reserved(name: str) -> bool:
    # Windows treats "NUL.txt" and "NUL " as the device too.
    return name.split(".", 1)[0].strip().upper() in RESERVED_NAMES


def sanitize_component(value: str, max_len: int = MAX_COMPONENT_LEN) -> str:
    """Turn arbitrary text into a single safe folder/file name. Never raises, never empty."""
    text = unicodedata.normalize("NFC", value)
    text = _INVALID_CHARS.sub("_", text).strip().rstrip(". ")
    if not text or set(text) <= {"."}:
        return "_"
    if _is_reserved(text):
        text = f"_{text}"
    return text[:max_len].rstrip(". ") or "_"


def check_component(name: str) -> None:
    """Reject a single path component that Windows would treat specially or that is unsafe."""
    if not name or name in {".", ".."}:
        raise PathSafetyError(f"Invalid path component: {name!r}")
    if ":" in name:
        raise PathSafetyError(f"Alternate data streams / drive syntax not allowed: {name!r}")
    if _INVALID_CHARS.search(name):
        raise PathSafetyError(f"Illegal character in path component: {name!r}")
    if name != name.rstrip(". "):
        raise PathSafetyError(f"Component ends with a dot or space: {name!r}")
    if _is_reserved(name):
        raise PathSafetyError(f"Reserved Windows device name: {name!r}")


def _check_lexically(path: str) -> PureWindowsPath:
    """Check the path text itself, before the OS collapses '..' or resolves anything."""
    if "\x00" in path:
        raise PathSafetyError("NUL byte in path")
    pure = PureWindowsPath(path)
    parts = pure.parts[1:] if pure.anchor else pure.parts
    for part in parts:
        check_component(part)
    return pure


def ensure_absolute_windows_path(path: str) -> PureWindowsPath:
    """Validate a configured root: absolute (drive letter or UNC), no '..', no ADS."""
    pure = _check_lexically(path)
    if not pure.is_absolute():
        raise PathSafetyError(
            f"Path must be absolute (X:\\... or \\\\server\\share\\...): {path!r}"
        )
    if path.startswith(("\\\\?\\", "\\\\.\\")):
        raise PathSafetyError("Device/extended-length prefixes are added by the app, not typed")
    if pure.drive.startswith("\\\\") and not _UNC_SHARE.match(pure.drive):
        raise PathSafetyError(f"UNC path needs both server and share: {path!r}")
    return pure


def _real(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.realpath(path))


def resolve_within(root: str | os.PathLike[str], candidate: str | os.PathLike[str]) -> Path:
    """Return the real path of ``candidate`` if, after resolving symlinks and junctions,
    it is ``root`` or lies inside it. Otherwise raise :class:`PathSafetyError`."""
    _check_lexically(os.fspath(candidate))
    real_root = _real(root)
    real_cand = _real(candidate)
    try:
        inside = os.path.commonpath([real_root, real_cand]) == real_root
    except ValueError:  # different drives, or mixed absolute/relative
        inside = False
    if not inside:
        raise PathSafetyError(
            f"{os.fspath(candidate)!r} is outside the allowed root {os.fspath(root)!r}"
        )
    return Path(os.path.realpath(candidate))


def resolve_anywhere(candidate: str | os.PathLike[str]) -> Path:
    """The real path of a folder or file the user picked by hand in the file dialog, for a
    department that may take content from anywhere. The text must be an absolute Windows path
    (no ``..``, no alternate streams, no device prefixes) and must not be a whole drive or share.
    Text from an email never gets here: only the user's own pick in the dialog does."""
    text = os.fspath(candidate)
    ensure_absolute_windows_path(text)
    real = Path(os.path.realpath(text))
    if str(real).rstrip("\\/") == str(Path(real.anchor)).rstrip("\\/"):
        raise PathSafetyError("Choose a folder or file inside the drive, not the whole drive")
    return real


def resolve_within_any(
    roots: Iterable[str | os.PathLike[str]], candidate: str | os.PathLike[str]
) -> Path:
    """Like :func:`resolve_within`, but the candidate may sit under any one of ``roots``."""
    for root in roots:
        try:
            return resolve_within(root, candidate)
        except PathSafetyError:
            continue
    raise PathSafetyError(f"{os.fspath(candidate)!r} is not inside any configured folder")


def join_within(root: str | os.PathLike[str], *parts: str) -> Path:
    """Join text parts under ``root`` and verify the result stays inside it.

    Each part may contain separators (``"ProgramX/2026-10-12"``); every component is sanitised,
    so email-derived text cannot introduce ``..`` or absolute paths.
    """
    components: list[str] = []
    for part in parts:
        components.extend(sanitize_component(piece) for piece in _SEPARATORS.split(part) if piece)
    if not components:
        raise PathSafetyError("Nothing to join")
    return resolve_within(root, Path(root).joinpath(*components))

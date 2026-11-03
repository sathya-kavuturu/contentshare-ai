from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from contentshare.security.paths import (
    PathSafetyError,
    check_component,
    ensure_absolute_windows_path,
    join_within,
    resolve_within,
    sanitize_component,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Program X", "Program X"),
        ("a/b\\c", "a_b_c"),
        ("..", "_"),
        ("...", "_"),
        ("", "_"),
        ("   ", "_"),
        ("name. ", "name"),
        ("CON", "_CON"),
        ("nul.txt", "_nul.txt"),
        ("COM1", "_COM1"),
        ("LPT¹", "_LPT¹"),
        ('we<ird>:na"me|?*', "we_ird__na_me___"),
        ("tab\there", "tab_here"),
        ("file.txt:stream", "file.txt_stream"),
    ],
)
def test_sanitize_component(raw: str, expected: str) -> None:
    assert sanitize_component(raw) == expected


def test_sanitize_truncates_and_keeps_unicode() -> None:
    assert sanitize_component("x" * 500) == "x" * 120
    assert sanitize_component("कार्यक्रम 🎬") == "कार्यक्रम 🎬"


@given(st.text())
def test_sanitize_output_always_passes_check_component(raw: str) -> None:
    check_component(sanitize_component(raw))


@pytest.mark.parametrize("name", ["", ".", "..", "a:b", "a<b", "NUL", "nul.log", "trail.", "sp "])
def test_check_component_rejects(name: str) -> None:
    with pytest.raises(PathSafetyError):
        check_component(name)


@pytest.mark.parametrize(
    "path",
    [r"Z:\media\Incoming", r"\\nas02\media\Incoming", "D:/x/y"],
)
def test_absolute_paths_accepted(path: str) -> None:
    ensure_absolute_windows_path(path)


@pytest.mark.parametrize(
    "path",
    [
        r"relative\path",
        r"C:\a\..\b",
        r"C:\a\file.txt:stream",
        r"\\?\C:\long",
        r"C:\a\CON",
        r"\\server",
        "C:\\a\x00b",
    ],
)
def test_absolute_paths_rejected(path: str) -> None:
    with pytest.raises(PathSafetyError):
        ensure_absolute_windows_path(path)


def test_resolve_within_accepts_inside(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    assert (
        resolve_within(tmp_path, tmp_path / "a" / "new.txt")
        == (tmp_path / "a" / "new.txt").resolve()
    )
    assert resolve_within(tmp_path, tmp_path) == tmp_path.resolve()


def test_resolve_within_rejects_traversal_and_siblings(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "root-evil").mkdir()
    with pytest.raises(PathSafetyError):
        resolve_within(root, str(root) + "\\..\\other")
    with pytest.raises(PathSafetyError):
        resolve_within(root, tmp_path / "root-evil" / "x")  # shares a name prefix, not a parent
    with pytest.raises(PathSafetyError):
        resolve_within(root, tmp_path)


def test_resolve_within_rejects_ads_and_reserved(tmp_path: Path) -> None:
    with pytest.raises(PathSafetyError):
        resolve_within(tmp_path, tmp_path / "file.txt:hidden")
    with pytest.raises(PathSafetyError):
        resolve_within(tmp_path, tmp_path / "NUL")


def test_resolve_within_rejects_junction_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "link"
    made = subprocess.run(  # noqa: S603
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)],  # noqa: S607
        capture_output=True,
        check=False,
    )
    if made.returncode != 0:
        pytest.skip("cannot create a junction here")
    with pytest.raises(PathSafetyError):
        resolve_within(root, link / "secret.txt")


@pytest.mark.parametrize(
    "hostile",
    ["..", "../..", r"..\..\Windows", "C:\\Windows", "/etc/passwd", "a/../../b", "CON", "x:y"],
)
def test_join_within_neutralises_email_derived_text(tmp_path: Path, hostile: str) -> None:
    result = join_within(tmp_path, hostile)
    assert result == resolve_within(tmp_path, result)  # inside root, no exception


@given(st.text(max_size=80))
def test_join_within_never_escapes(text: str) -> None:
    root = Path(__file__).parent
    try:
        result = join_within(root, text, "2026-10-12")
    except PathSafetyError:
        return  # refusing is always acceptable
    resolve_within(root, result)


def test_join_within_empty_raises(tmp_path: Path) -> None:
    with pytest.raises(PathSafetyError):
        join_within(tmp_path, "")

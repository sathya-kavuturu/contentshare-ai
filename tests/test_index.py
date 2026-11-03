from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from contentshare.config.models import IndexSettings
from contentshare.index import ContentIndex, measure, query_tokens
from contentshare.storage.db import MIGRATIONS, Database, _split_script
from helpers import make_storage


@pytest.fixture
def storage(tmp_path: Path) -> Path:
    return make_storage(tmp_path / "Programs")


@pytest.fixture
def index(db: Database) -> ContentIndex:
    return ContentIndex(db)


def names(index: ContentIndex, text: str, *, fuzzy: bool = True) -> list[str]:
    return [i.name for i in index.search(text, fuzzy=fuzzy)]


def test_scan_indexes_names_sizes_and_counts(index: ContentIndex, storage: Path) -> None:
    report = index.scan_root(str(storage))
    assert report.status == "complete"
    assert report.errors == 0
    folder = index.find_path(str(storage), "2026/2026-10-12_SunriseTalk_EN")
    assert folder is not None
    assert folder.is_dir
    assert (folder.size, folder.file_count) == (2510, 3)  # includes the nested notes/ folder
    top = index.find_path(str(storage), "2026")
    assert top is not None
    assert top.file_count == 7
    assert top.depth == 1
    video = index.find_path(str(storage), "2026/2026-10-12_SunriseTalk_EN/video.mxf")
    assert video is not None
    assert (video.kind, video.size, video.file_count) == ("file", 2000, 1)
    assert video.full_path == str(storage) + "\\2026\\2026-10-12_SunriseTalk_EN\\video.mxf"


def test_system_and_junk_names_are_excluded(index: ContentIndex, storage: Path) -> None:
    index.scan_root(str(storage))
    assert index.find_path(str(storage), "$RECYCLE.BIN") is None
    assert index.find_path(str(storage), "Thumbs.db") is None
    assert index.find_path(str(storage), "$RECYCLE.BIN/junk.txt") is None
    root_item = index.find_path(str(storage), "2026")
    assert root_item is not None


def test_custom_excludes_and_folders_only(db: Database, storage: Path) -> None:
    custom = ContentIndex(db, IndexSettings(exclude_names=["notes", "*.wav"], index_files=False))
    custom.scan_root(str(storage))
    assert custom.find_path(str(storage), "2026/2026-10-12_SunriseTalk_EN/notes") is None
    assert (
        custom.find_path(str(storage), "2026/2026-10-12_SunriseTalk_EN/video.mxf") is None
    )  # files off
    folder = custom.find_path(str(storage), "2026/2026-10-12_SunriseTalk_EN")
    assert folder is not None
    assert (folder.size, folder.file_count) == (2000, 1)  # excluded files do not count either


def test_rescan_without_changes_is_stable(index: ContentIndex, storage: Path) -> None:
    index.scan_root(str(storage))
    before = index.stats()
    again = index.scan_root(str(storage))
    assert again.status == "complete"
    assert index.stats() == before
    assert names(index, "sunrise talk")  # search index still consistent


def test_new_folder_deep_in_the_tree_is_found(index: ContentIndex, storage: Path) -> None:
    """A folder's modified time does not change when something is added several levels below,
    so a scan must look at every folder, not skip the ones whose time is unchanged."""
    index.scan_root(str(storage))
    assert names(index, "Quantum", fuzzy=False) == []
    deep = storage / "2026" / "2026-10-12_SunriseTalk_EN" / "notes" / "QuantumLecture_2026-11-01"
    deep.mkdir()
    (deep / "talk.mp4").write_bytes(b"y" * 4000)

    index.scan_root(str(storage))

    assert "QuantumLecture_2026-11-01" in names(index, "Quantum lecture")
    parent = index.find_path(str(storage), "2026/2026-10-12_SunriseTalk_EN")
    assert parent is not None
    assert (parent.size, parent.file_count) == (6510, 4)  # totals above it were updated too


def test_removed_content_leaves_the_index_but_never_the_server(
    index: ContentIndex, storage: Path
) -> None:
    index.scan_root(str(storage))
    gone = storage / "2025" / "AnnualDay_2025-12-25"
    for p in sorted(gone.rglob("*"), reverse=True):
        p.unlink() if p.is_file() else p.rmdir()
    gone.rmdir()
    index.scan_root(str(storage))
    assert names(index, "Annual", fuzzy=False) == []
    assert index.find_path(str(storage), "2025/AnnualDay_2025-12-25/video/annual.mp4") is None
    assert (storage / "2026").is_dir()  # the scanner only ever reads


def test_changed_file_size_is_updated(index: ContentIndex, storage: Path) -> None:
    index.scan_root(str(storage))
    (storage / "2026/2026-10-12_SunriseTalk_HI/video.mxf").write_bytes(b"z" * 9999)
    index.scan_root(str(storage))
    folder = index.find_path(str(storage), "2026/2026-10-12_SunriseTalk_HI")
    assert folder is not None
    assert folder.size == 9999


def test_unreachable_root_fails_without_touching_the_index(
    index: ContentIndex, storage: Path, tmp_path: Path
) -> None:
    index.scan_root(str(storage))
    before = index.stats()
    report = index.scan_root(str(tmp_path / "no-such-server"))
    assert report.status == "failed"
    assert "Cannot reach" in (report.error or "")
    # the same root becoming unreachable later must not empty the index either
    moved = storage.with_name("Programs_offline")
    storage.rename(moved)
    assert index.scan_root(str(storage)).status == "failed"
    assert index.stats() == before


def test_cancelled_scan_keeps_what_was_there(index: ContentIndex, storage: Path) -> None:
    index.scan_root(str(storage))
    before = index.stats()
    (storage / "NewFolder").mkdir()
    seen = {"n": 0}

    def stop_soon() -> bool:
        seen["n"] += 1
        return seen["n"] > 3

    report = index.scan_root(str(storage), should_stop=stop_soon)
    assert report.status == "cancelled"
    assert index.stats()["items"] >= before["items"]  # nothing was deleted


def test_unreadable_subfolder_keeps_its_old_entries(
    index: ContentIndex, storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index.scan_root(str(storage))
    before = index.stats()
    real_scandir = os.scandir

    def flaky(path: str):  # type: ignore[no-untyped-def]
        if "2025" in str(path) and "AnnualDay" not in str(path):
            raise PermissionError("share went away")
        return real_scandir(path)

    monkeypatch.setattr("contentshare.index.store.os.scandir", flaky)
    report = index.scan_root(str(storage))
    assert report.status == "complete"
    assert report.errors >= 1
    assert index.stats() == before  # the unreadable folder's entries were kept
    assert names(index, "Annual Day")


def test_junctions_are_not_indexed(index: ContentIndex, storage: Path, tmp_path: Path) -> None:
    outside = make_storage(tmp_path / "elsewhere", {"Secret/secret.txt": 7})
    made = subprocess.run(  # noqa: S603
        ["cmd", "/c", "mklink", "/J", str(storage / "linked"), str(outside)],  # noqa: S607
        capture_output=True,
        check=False,
    )
    if made.returncode != 0:
        pytest.skip("cannot create a junction here")
    index.scan_root(str(storage))
    assert index.find_path(str(storage), "linked") is None
    assert names(index, "secret", fuzzy=False) == []


def test_progress_is_reported(index: ContentIndex, storage: Path) -> None:
    big = make_storage(storage / "bulk", {f"f{i % 7}/file{i}.txt": 1 for i in range(1200)})
    seen: list[int] = []
    index.scan_root(str(big.parent), progress=lambda n, _p: seen.append(n))
    assert seen
    assert seen == sorted(seen)


def test_forget_root_and_last_scan(index: ContentIndex, storage: Path, tmp_path: Path) -> None:
    other = make_storage(tmp_path / "Other", {"ZetaLecture/a.txt": 1})
    index.scan_root(str(storage))
    index.scan_root(str(other))
    assert "ZetaLecture" in names(index, "zeta lecture", fuzzy=False)
    scan = index.last_scan(str(storage))
    assert scan is not None
    assert scan["status"] == "complete"
    assert index.last_scan(str(tmp_path / "never")) is None
    index.forget_root(str(other))
    assert names(index, "zeta lecture", fuzzy=False) == []
    assert names(index, "Sunrise")  # the other root is untouched


# ------------------------------------------------------------------ searching
def test_search_finds_by_words_prefix_and_accents(index: ContentIndex, storage: Path) -> None:
    index.scan_root(str(storage))
    assert "2026-10-12_SunriseTalk_EN" in names(index, "Sunrise Talk")
    assert "2026-10-12_SunriseTalk_EN" in names(index, "sunrise")
    assert any("Caf" in n for n in names(index, "cafe satsang"))  # accent-insensitive
    assert "YouthRetreat_2026-09-04_Day2" in names(index, "youth retreat")


def test_search_tolerates_a_typo_in_the_middle_of_a_word(
    index: ContentIndex, storage: Path
) -> None:
    index.scan_root(str(storage))
    assert "2026-10-12_SunriseTalk_EN" in names(index, "Sunrize Talk")


def test_search_name_hits_come_before_files_inside(index: ContentIndex, storage: Path) -> None:
    index.scan_root(str(storage))
    hits = index.search("Annual Day")
    assert hits[0].name == "AnnualDay_2025-12-25"


@pytest.mark.parametrize(
    "hostile",
    [
        '"',
        "*",
        "OR",
        "NEAR(",
        "a AND",
        '"; DROP TABLE content_items; --',
        "name:",
        "{",
        "^x",
        "-x",
        "(",
        "\\",
    ],
)
def test_search_cannot_be_broken_by_odd_text(
    index: ContentIndex, storage: Path, hostile: str
) -> None:
    index.scan_root(str(storage))
    assert isinstance(index.search(hostile), list)
    assert index.stats()["items"] > 0  # nothing was dropped


def test_search_empty_and_limits(index: ContentIndex, storage: Path) -> None:
    index.scan_root(str(storage))
    assert index.search("") == []
    assert index.search("   ") == []
    assert len(index.search("talk", limit=2)) <= 2


def test_query_tokens() -> None:
    assert query_tokens("SunriseTalk_2026-10-12!") == ["sunrise", "talk", "2026", "10", "12"]
    assert query_tokens("a b  cc cc") == ["cc"]
    assert len(query_tokens(" ".join(f"word{i}" for i in range(50)))) == 8


def test_get_and_stats(index: ContentIndex, storage: Path) -> None:
    index.scan_root(str(storage))
    item = index.search("Sunrise")[0]
    assert index.get(item.id) == item
    assert index.get(10_000_000) is None
    stats = index.stats()
    assert stats["folders"] < stats["items"]


# ------------------------------------------------------------------ measure()
def test_measure_folder_file_and_missing(storage: Path, tmp_path: Path) -> None:
    assert measure(storage / "2026" / "2026-10-12_SunriseTalk_EN") == (2510, 3)
    assert measure(storage / "2026" / "2026-10-12_SunriseTalk_EN" / "video.mxf") == (2000, 1)
    assert measure(tmp_path / "gone") is None


# ------------------------------------------------------------------ migration
def test_upgrade_from_v2_database_adds_the_index(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    for script in MIGRATIONS[:2]:
        for statement in _split_script(script):
            conn.execute(statement)
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    db = Database(path)
    index = ContentIndex(db)
    assert index.stats() == {"items": 0, "folders": 0}
    with db.read() as c:
        tables = {r["name"] for r in c.execute("SELECT name FROM sqlite_master")}
    assert {"content_items", "content_fts", "index_scans"} <= tables

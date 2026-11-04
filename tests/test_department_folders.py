"""A department's own folders to search, and the whole-PC fallback."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QFileDialog

from contentshare.app.controller import ActionError
from contentshare.config.models import AppConfig, Department
from contentshare.index import pcsearch
from contentshare.index.pcsearch import SKIP_NAMES, search_pc
from contentshare.jobs.copy_job import resolve_source
from contentshare.jobs.states import RequestState as S
from contentshare.security.paths import PathSafetyError
from contentshare.ui.dialogs import CandidateDialog
from contentshare.ui.settings_page import DepartmentDialog
from helpers import make_storage
from test_controller import Harness, h  # noqa: F401  (the controller fixture)
from test_feedback_round import rig  # noqa: F401  (the window fixture)

OWN = {"Kirtan/ZetaKirtan_2026-05-01_EN/clip.mp4": 400, "Kirtan/ZetaKirtan_2026-05-01_EN/a.txt": 20}


def _dept(**extra: Any) -> Department:
    return Department(
        name="Media Team", sender_emails=["anita@dept.org"], destination_root="D:\\Out", **extra
    )


def _own_root(h: Harness, tmp_path: Path, **dept: Any) -> Path:  # noqa: F811
    """Give Media Team its own folder (with ZetaKirtan in it), indexed."""
    own = make_storage(tmp_path / "MediaArchive", OWN)
    departments = [
        d.model_copy(update={"source_roots": [str(own)], **dept}) if d.name == "Media Team" else d
        for d in h.ctl.config.departments
    ]
    h.ctl.apply_config(h.ctl.config.model_copy(update={"departments": departments}), persist=False)
    h.ctl.rescan_index()
    return own


# --------------------------------------------------------------------------- the settings
def test_a_department_has_no_own_folders_and_no_pc_search_by_default() -> None:
    d = _dept()
    assert d.source_roots == [] and d.search_whole_pc is False and not d.may_pick_anywhere
    assert Department.model_validate({**d.model_dump(), "browse_anywhere": True}).may_pick_anywhere
    assert _dept(search_whole_pc=True).may_pick_anywhere  # a pick from the PC search needs it


def test_old_settings_files_still_load_and_everyone_uses_the_general_folders() -> None:
    config = AppConfig.model_validate(
        {
            "source_roots": ["D:\\Programs"],
            "departments": [
                {"name": "M", "sender_emails": ["a@d.org"], "destination_root": "E:\\Out"}
            ],
        }
    )
    assert config.roots_for(config.departments[0]) == ["D:\\Programs"]
    assert config.roots_for(None) == ["D:\\Programs"]
    assert config.all_roots() == ["D:\\Programs"]


def test_a_department_with_its_own_folders_searches_only_those() -> None:
    config = AppConfig(
        source_roots=["D:\\General"],
        departments=[
            _dept(source_roots=["\\\\nas01\\Media", "E:\\Extra"]),
            {
                "name": "Finance",
                "sender_emails": ["f@dept.org"],
                "destination_root": "D:\\Fin",
                "source_roots": ["E:\\extra\\"],  # the same folder as above, written differently
            },
        ],
    )
    media, finance = config.departments
    assert config.roots_for(media) == ["\\\\nas01\\Media", "E:\\Extra"]
    assert config.roots_for(finance) == ["E:\\extra"]
    assert config.all_roots() == ["D:\\General", "\\\\nas01\\Media", "E:\\Extra"]  # each once


def test_own_folders_are_cleaned_and_must_be_absolute() -> None:
    assert _dept(source_roots=[" E:\\A\\ ", "E:\\a", "E:\\B"]).source_roots == ["E:\\A", "E:\\B"]
    with pytest.raises(ValueError, match=r"absolute|drive|full"):
        _dept(source_roots=["relative\\folder"])


def test_no_department_may_copy_into_any_folder_that_is_searched() -> None:
    with pytest.raises(ValueError, match="overlaps"):
        AppConfig(
            departments=[
                _dept(source_roots=["E:\\Archive"]),
                {
                    "name": "Finance",
                    "sender_emails": ["f@dept.org"],
                    "destination_root": "E:\\Archive\\Finance",  # inside Media's archive
                },
            ]
        )


# --------------------------------------------------------------------------- the index
def test_a_search_can_be_limited_to_some_storage_folders(h: Harness, tmp_path: Path) -> None:  # noqa: F811
    own = _own_root(h, tmp_path)
    general = str(h.world.storage)
    index = h.world.index
    assert {i.root for i in index.search("ZetaKirtan", fuzzy=False)} == {str(own)}
    assert index.search("ZetaKirtan", roots=[general], fuzzy=False) == []
    assert {i.root for i in index.search("ZetaKirtan", roots=[str(own)])} == {str(own)}
    assert index.search("ZetaKirtan", roots=[]) == []  # no folders: nothing
    assert index.search("SunriseTalk", roots=[str(own)]) == []
    assert index.search("SunriseTalk", roots=[general])
    # the typo-tolerant fallback is limited too
    assert {i.root for i in index.search("Zetakirtn", roots=[general])} <= {general}
    assert index.find_dirs_named(["ZetaKirtan_2026-05-01_EN"], [general]) == {}
    assert index.find_dirs_named(["zetakirtan_2026-05-01_en"], [str(own)])


# --------------------------------------------------------------------------- requests
def test_a_request_is_looked_for_only_in_its_departments_folders(
    h: Harness,  # noqa: F811
    tmp_path: Path,
) -> None:
    own = _own_root(h, tmp_path)
    h.mail(message_id="a1", subject="ZetaKirtan_2026-05-01_EN", body="Please share it")
    (found,) = h.ctl.views([S.READY])
    assert (found.source_path or "").startswith(str(own))

    # Media Team's own folder has no SunriseTalk: it is not found there, though the general
    # storage folder has it.
    h.mail(message_id="a2", subject="2026-10-12_SunriseTalk_EN", body="Please share it")
    reviews = h.ctl.views([S.NEEDS_REVIEW])
    assert len(reviews) == 1 and "No content found" in (reviews[0].reason or "")

    # Finance has no folders of its own, so it still uses the general ones.
    h.mail(
        message_id="a3",
        from_="Fin <accounts@finance.org>",
        subject="2026-10-12_SunriseTalk_EN",
        body="Please share it",
    )
    finance = [v for v in h.ctl.views() if v.department == "Finance"]
    assert finance and (finance[0].source_path or "").startswith(str(h.world.storage))


def test_exact_folder_names_are_looked_up_in_the_departments_folders_only(
    h: Harness,  # noqa: F811
    tmp_path: Path,
) -> None:
    own = _own_root(h, tmp_path, exact_folder_names=True)
    h.mail(
        message_id="e1",
        subject="Folders",
        body="- ZetaKirtan_2026-05-01_EN\n- 2026-10-12_SunriseTalk_EN",
    )
    states = {v.requested: v for v in h.ctl.views()}
    ready = [v for v in states.values() if v.state == "READY"]
    assert len(ready) == 1 and (ready[0].source_path or "").startswith(str(own))
    assert any("No folder named" in (v.reason or "") for v in states.values())


def test_a_pick_must_be_inside_the_departments_folders(h: Harness, tmp_path: Path) -> None:  # noqa: F811
    own = _own_root(h, tmp_path)
    h.mail(message_id="p1", subject="Quantum Lecture", body="need it")
    (view,) = h.ctl.views([S.NEEDS_REVIEW])
    general_folder = h.world.storage / "2025" / "AnnualDay_2025-12-25"
    with pytest.raises(ActionError, match="outside the storage folders"):
        h.ctl.choose_path(view.id, str(general_folder))  # in the general folders, not Media Team's
    h.ctl.choose_path(view.id, str(own / "Kirtan" / "ZetaKirtan_2026-05-01_EN"))
    assert h.ctl.view(view.id).state == "READY"


def test_resolve_source_follows_the_departments_folders(tmp_path: Path) -> None:
    general, own = tmp_path / "general", tmp_path / "own"
    for folder in (general, own):
        (folder / "x").mkdir(parents=True)
    config = AppConfig(source_roots=[str(general)], departments=[_dept(source_roots=[str(own)])])
    dept = config.departments[0]
    assert resolve_source(config, dept, str(own / "x")) == Path(os.path.realpath(own / "x"))
    with pytest.raises(PathSafetyError):
        resolve_source(config, dept, str(general / "x"))


def test_every_folder_is_indexed_and_a_removed_one_is_forgotten(h: Harness, tmp_path: Path) -> None:  # noqa: F811
    own = _own_root(h, tmp_path)
    reports = h.ctl.rescan_index()
    assert {r.root for r in reports} == {str(h.world.storage), str(own)}
    assert h.ctl.index_summary()["roots"] == 2
    config = h.ctl.config
    departments = [d.model_copy(update={"source_roots": []}) for d in config.departments]
    h.ctl.apply_config(config.model_copy(update={"departments": departments}), persist=False)
    assert h.world.index.search("ZetaKirtan", fuzzy=False) == []  # its rows went with the folder


# --------------------------------------------------------------------------- the PC search
def _pc(tmp_path: Path) -> Path:
    root = tmp_path / "pc"
    make_storage(
        root,
        {
            "Videos/Old/SunriseTalk_2026-10-12/a.mp4": 5,
            "Videos/SunriseTalk_2026-10-19.mp4": 5,
            "Documents/Taxes/readme.txt": 1,
            "node_modules/SunriseTalk_copy/x.js": 1,
            "$RECYCLE.BIN/SunriseTalk_deleted/x": 1,
            "Out/SunriseTalk_2026-10-12/already_copied.mp4": 5,
        },
    )
    return root


def test_the_pc_search_finds_folders_and_files_by_name_and_skips_what_it_should(
    tmp_path: Path,
) -> None:
    root = _pc(tmp_path)
    found = search_pc("SunriseTalk", drives=[str(root)], skip=[str(root / "Out")])
    paths = sorted(i.rel_path for i in found.items)
    assert paths == [
        "Videos/Old/SunriseTalk_2026-10-12",
        "Videos/SunriseTalk_2026-10-19.mp4",
    ]
    assert found.complete and found.folders_looked_at > 3
    assert {i.kind for i in found.items} == {"dir", "file"}
    assert found.items[0].full_path.startswith(str(root))
    assert all(name.casefold() == name for name in SKIP_NAMES)


def test_the_pc_search_needs_most_of_the_words_and_ignores_empty_requests(tmp_path: Path) -> None:
    root = _pc(tmp_path)
    assert search_pc("", drives=[str(root)]).items == []
    assert search_pc("Sunrise Taxes Zebra", drives=[str(root)]).items == []  # one of three words
    assert search_pc("Zebra", drives=[str(root)]).items == []
    assert search_pc("Sunrise Talk", drives=[str(root)]).items  # words of a name written apart


def test_the_pc_search_can_be_stopped_and_has_a_time_limit(tmp_path: Path) -> None:
    root = _pc(tmp_path)
    stopped = search_pc("SunriseTalk", drives=[str(root)], should_stop=lambda: True)
    assert not stopped.complete and stopped.items == []
    ticks = iter(range(0, 1000, 10))
    late = search_pc("SunriseTalk", drives=[str(root)], seconds=25, clock=lambda: next(ticks))
    assert not late.complete  # the clock ran out before every folder was looked at


def test_the_pc_search_does_not_follow_links(tmp_path: Path) -> None:
    root = _pc(tmp_path)
    outside = tmp_path / "elsewhere"
    (outside / "SunriseTalk_linked").mkdir(parents=True)
    try:
        (root / "Videos" / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are not allowed here")
    paths = [i.rel_path for i in search_pc("SunriseTalk", drives=[str(root)]).items]
    assert not any("linked" in p for p in paths)


def test_only_fixed_drives_are_listed() -> None:
    drives = pcsearch.fixed_drives()
    assert drives and all(len(d) == 3 and d[1:] == ":\\" for d in drives)


# --------------------------------------------------------------------------- the PC fallback
def _pc_department(h: Harness, tmp_path: Path, on: bool = True) -> Path:  # noqa: F811
    own = _own_root(h, tmp_path, search_whole_pc=on)
    return own


def test_the_whole_pc_is_searched_only_when_the_department_allows_it(
    h: Harness,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _pc(tmp_path)
    monkeypatch.setattr(pcsearch, "fixed_drives", lambda: [str(root)])
    _pc_department(h, tmp_path, on=False)
    h.mail(message_id="w1", subject="SunriseTalk 2026-10-12", body="need it")
    (view,) = h.ctl.views([S.NEEDS_REVIEW])
    assert not h.ctl.pc_search_allowed(view.id)
    with pytest.raises(ActionError, match="switched off"):
        h.ctl.search_pc(view.id)


def test_a_match_on_the_pc_is_only_a_suggestion_and_can_be_chosen(
    h: Harness,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _pc(tmp_path)
    monkeypatch.setattr(pcsearch, "fixed_drives", lambda: [str(root)])
    _pc_department(h, tmp_path)
    h.mail(message_id="w2", subject="SunriseTalk 2026-10-12", body="need it")
    (view,) = h.ctl.views([S.NEEDS_REVIEW])  # never made Ready by the PC search
    assert h.ctl.pc_search_allowed(view.id)
    found, complete = h.ctl.search_pc(view.id)
    assert complete and found and all(c.item_id is None for c in found)
    best = found[0]
    assert best.path.endswith("SunriseTalk_2026-10-12")
    assert h.ctl.view(view.id).state == "NEEDS_REVIEW"  # still waiting for the person
    h.ctl.choose_path(view.id, best.path)  # the pick is measured and checked again
    after = h.ctl.view(view.id)
    assert after.state == "READY" and after.source_path == best.path and after.file_count == 1


def test_the_pc_search_never_offers_the_departments_own_destination(
    h: Harness,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media = h.world.media
    (media / "SunriseTalk_2026-10-12").mkdir()
    monkeypatch.setattr(pcsearch, "fixed_drives", lambda: [str(media.parent)])
    _pc_department(h, tmp_path)
    h.mail(message_id="w3", subject="SunriseTalk 2026-10-12", body="need it")
    (view,) = h.ctl.views([S.NEEDS_REVIEW])
    found, _complete = h.ctl.search_pc(view.id)
    assert not any(str(media) in c.path for c in found)


def test_a_pick_from_the_pc_is_still_refused_inside_windows(
    h: Harness,  # noqa: F811
    tmp_path: Path,
) -> None:
    _pc_department(h, tmp_path)
    h.mail(message_id="w4", subject="Quantum Lecture", body="need it")
    (view,) = h.ctl.views([S.NEEDS_REVIEW])
    system = os.environ.get("SYSTEMROOT", "C:\\Windows")
    with pytest.raises(ActionError, match="cannot be used"):
        h.ctl.choose_path(view.id, str(Path(system) / "System32"))


# --------------------------------------------------------------------------- the screens
def test_the_department_dialog_saves_its_own_folders_and_the_pc_option(qtbot: Any) -> None:
    dialog = DepartmentDialog(None)
    qtbot.addWidget(dialog)
    assert dialog.folders.paths() == [] and not dialog.whole_pc.isChecked()
    dialog.name.setText("Media")
    dialog.emails.setPlainText("a@dept.org")
    dialog.root.setText("D:\\Out")
    dialog.folders.add("\\\\nas01\\Media")
    dialog.folders.add("E:\\Extra")
    dialog.whole_pc.setChecked(True)
    dialog._accept()
    saved = dialog.result_department
    assert saved is not None and saved.source_roots == ["\\\\nas01\\Media", "E:\\Extra"]
    assert saved.search_whole_pc
    again = DepartmentDialog(saved)
    qtbot.addWidget(again)
    assert again.folders.paths() == saved.source_roots and again.whole_pc.isChecked()


def test_the_departments_table_says_where_each_one_searches(qtbot: Any, rig: Any) -> None:  # noqa: F811
    page = rig.window.settings
    assert page.dept_table.columnCount() == 5
    page.departments[0] = page.departments[0].model_copy(
        update={"source_roots": ["E:\\Extra"], "search_whole_pc": True}
    )
    page._fill_departments()
    text = page.dept_table.item(0, 4).text()
    assert "E:\\Extra" in text and "this PC" in text
    assert page.dept_table.item(1, 4).text() == "General storage folders"


def _review(h: Harness) -> Any:  # noqa: F811
    h.mail(message_id="d1", subject="Quantum Lecture", body="need it")
    (view,) = h.ctl.views([S.NEEDS_REVIEW])
    return view


def test_the_choose_dialog_offers_the_pc_search_only_to_departments_that_allow_it(
    h: Harness,  # noqa: F811
    qtbot: Any,
    tmp_path: Path,
) -> None:
    view = _review(h)
    plain = CandidateDialog(h.ctl, view)
    qtbot.addWidget(plain)
    assert plain.pc_button.isHidden()
    _pc_department(h, tmp_path)
    allowed = CandidateDialog(h.ctl, view)
    qtbot.addWidget(allowed)
    allowed._pc_stop.set()  # (it started by itself: nothing was suggested)
    assert not allowed.pc_button.isHidden()


def test_the_choose_dialog_searches_the_pc_by_itself_when_nothing_was_found(
    h: Harness,  # noqa: F811
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _pc(tmp_path)
    monkeypatch.setattr(pcsearch, "fixed_drives", lambda: [str(root)])
    _pc_department(h, tmp_path)
    h.mail(message_id="d2", subject="SunriseTalk 2026-10-12", body="need it")
    (view,) = h.ctl.views([S.NEEDS_REVIEW])
    assert not view.candidates  # nothing in Media Team's own folder
    dialog = CandidateDialog(h.ctl, view)
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog.results.count() > 0 and not dialog._pc_running, timeout=15000)
    assert (
        "this PC" in dialog.hint.text()
        and "SunriseTalk_2026-10-12" in dialog.results.item(0).text()
    )
    assert dialog.pc_button.text() == "Search the whole PC" and dialog.pc_button.isEnabled()
    dialog.accept()  # a match on the PC is chosen by its path (it is not in the index)
    assert dialog.chosen is None and (dialog.chosen_path or "").endswith("SunriseTalk_2026-10-12")


def test_the_choose_dialog_says_when_the_pc_has_nothing_either(
    h: Harness,  # noqa: F811
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "emptypc"
    root.mkdir()
    monkeypatch.setattr(pcsearch, "fixed_drives", lambda: [str(root)])
    _pc_department(h, tmp_path)
    view = _review(h)
    dialog = CandidateDialog(h.ctl, view)
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: not dialog._pc_running, timeout=15000)
    assert "Nothing like that was found on this PC" in dialog.hint.text()


def test_the_pc_search_can_be_stopped_from_the_dialog(
    h: Harness,  # noqa: F811
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def slow(program: str, **kwargs: Any) -> Any:
        stop = kwargs["should_stop"]
        while not stop():
            time.sleep(0.01)
        return pcsearch.PcSearchResult([], False, 0)

    monkeypatch.setattr("contentshare.app.controller.search_pc", slow)
    _pc_department(h, tmp_path)
    view = _review(h)
    dialog = CandidateDialog(h.ctl, view)
    qtbot.addWidget(dialog)
    assert dialog._pc_running and dialog.pc_button.text() == "Stop searching"
    dialog.pc_button.click()
    qtbot.waitUntil(lambda: not dialog._pc_running, timeout=10000)
    assert (
        "stopped before the whole PC" in dialog.hint.text()
        or "Nothing like that" in dialog.hint.text()
    )


def test_browsing_starts_in_the_departments_own_folder(
    h: Harness,  # noqa: F811
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    own = _own_root(h, tmp_path)
    view = _review(h)
    asked: list[str] = []

    def fake(_parent: Any, _title: str, start: str = "", *_a: Any, **_k: Any) -> str:
        asked.append(start)
        return ""

    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(fake))
    dialog = CandidateDialog(h.ctl, view)
    qtbot.addWidget(dialog)
    dialog.browse_folder.click()
    assert asked == [str(own)]

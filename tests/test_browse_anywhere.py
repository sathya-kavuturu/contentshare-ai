"""A department that may take content from anywhere: hand-picked folders and files."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QFileDialog

from contentshare.app.controller import ActionError
from contentshare.config.models import AppConfig, Department
from contentshare.jobs.copy_job import resolve_source
from contentshare.jobs.states import RequestState as S
from contentshare.security.paths import PathSafetyError, resolve_anywhere
from contentshare.ui.dialogs import CandidateDialog
from contentshare.ui.settings_page import DepartmentDialog
from contentshare.ui.wizard import FirstRunWizard
from test_controller import Harness, h  # noqa: F401  (the controller fixture)
from test_feedback_round import rig  # noqa: F401  (the window fixture)


def _anywhere(h: Harness, on: bool = True) -> None:  # noqa: F811
    departments = [d.model_copy(update={"browse_anywhere": on}) for d in h.ctl.config.departments]
    h.ctl.apply_config(h.ctl.config.model_copy(update={"departments": departments}), persist=False)


def _review(h: Harness) -> Any:  # noqa: F811
    h.mail(message_id="a1", subject="Quantum Lecture", body="need it")
    (view,) = h.ctl.views([S.NEEDS_REVIEW])
    return view


def _elsewhere(tmp_path: Path) -> Path:
    folder = tmp_path / "other_place" / "Some Program 2024"
    (folder / "sub").mkdir(parents=True)
    (folder / "a.mp4").write_bytes(b"a" * 40)
    (folder / "sub" / "b.txt").write_bytes(b"b" * 10)
    return folder


# --------------------------------------------------------------------------- the rule
def test_outside_the_storage_is_refused_unless_the_department_allows_it(
    h: Harness,  # noqa: F811
    tmp_path: Path,
) -> None:
    view = _review(h)
    folder = _elsewhere(tmp_path)
    with pytest.raises(ActionError, match="Allow choosing from anywhere"):
        h.ctl.choose_path(view.id, str(folder))
    assert h.ctl.view(view.id).state == "NEEDS_REVIEW"


def test_a_folder_from_anywhere_becomes_ready_and_goes_to_the_department_folder(
    h: Harness,  # noqa: F811
    tmp_path: Path,
) -> None:
    _anywhere(h)
    view = _review(h)
    folder = _elsewhere(tmp_path)
    h.ctl.choose_path(view.id, str(folder))
    after = h.ctl.view(view.id)
    assert after.state == "READY" and after.source_path == str(folder)
    assert after.file_count == 2 and after.total_bytes == 50
    assert Path(after.destination_path or "").parent == h.world.media
    assert Path(after.destination_path or "").name == "Some Program 2024"


def test_a_single_file_from_anywhere_goes_straight_into_the_department_folder(
    h: Harness,  # noqa: F811
    tmp_path: Path,
) -> None:
    _anywhere(h)
    view = _review(h)
    file = _elsewhere(tmp_path) / "a.mp4"
    h.ctl.choose_path(view.id, str(file))
    after = h.ctl.view(view.id)
    assert after.state == "READY" and after.file_count == 1
    assert (
        Path(after.destination_path or "")
        == Path(h.ctl.config.departments[0].destination_root).resolve()
    )


def test_something_inside_the_storage_still_works_as_before(h: Harness) -> None:  # noqa: F811
    _anywhere(h)
    view = _review(h)
    h.ctl.choose_path(view.id, str(h.world.storage / "2025" / "AnnualDay_2025-12-25"))
    assert h.ctl.view(view.id).state == "READY"


def test_windows_folders_and_whole_drives_are_never_taken(h: Harness) -> None:  # noqa: F811
    _anywhere(h)
    view = _review(h)
    system = os.environ.get("SYSTEMROOT", r"C:\Windows")
    with pytest.raises(ActionError, match="cannot be used"):
        h.ctl.choose_path(view.id, str(Path(system) / "System32"))
    with pytest.raises(ActionError, match="cannot be used"):
        h.ctl.choose_path(view.id, Path(system).anchor)
    with pytest.raises(ActionError, match="cannot be used"):
        h.ctl.choose_path(view.id, "relative\\folder")
    with pytest.raises(ActionError, match="cannot be used"):
        h.ctl.choose_path(view.id, str(h.world.media / ".." / ".." / "x"))
    assert h.ctl.view(view.id).state == "NEEDS_REVIEW"


def test_the_destination_folder_cannot_be_the_source(h: Harness, tmp_path: Path) -> None:  # noqa: F811
    _anywhere(h)
    view = _review(h)
    inside = h.world.media / "already_here"
    inside.mkdir()
    with pytest.raises(ActionError, match="destination folder"):
        h.ctl.choose_path(view.id, str(inside))
    with pytest.raises(ActionError, match="destination folder"):
        h.ctl.choose_path(view.id, str(h.world.media.parent))  # a folder that contains it


def test_a_missing_path_is_refused(h: Harness, tmp_path: Path) -> None:  # noqa: F811
    _anywhere(h)
    view = _review(h)
    with pytest.raises(ActionError, match="could not be found"):
        h.ctl.choose_path(view.id, str(tmp_path / "nothing_here"))


def test_the_pick_can_be_approved_and_copied_with_every_file_checked(
    h: Harness,  # noqa: F811
    tmp_path: Path,
) -> None:
    _anywhere(h)
    view = _review(h)
    folder = _elsewhere(tmp_path)
    h.ctl.choose_path(view.id, str(folder))
    h.ctl.approve(view.id, confirmed=True)
    h.wait_copy(view.id)
    after = h.ctl.view(view.id)
    assert after.state == "VERIFIED"
    copied = Path(after.destination_path or "")
    assert (copied / "a.mp4").read_bytes() == b"a" * 40
    assert (copied / "sub" / "b.txt").read_bytes() == b"b" * 10
    assert (folder / "a.mp4").exists()  # the original is untouched


def test_turning_the_option_off_stops_an_outside_pick_from_being_copied(
    h: Harness,  # noqa: F811
    tmp_path: Path,
) -> None:
    _anywhere(h)
    view = _review(h)
    h.ctl.choose_path(view.id, str(_elsewhere(tmp_path)))
    _anywhere(h, on=False)
    with pytest.raises(ActionError, match="Path not allowed"):
        h.ctl.approve(view.id, confirmed=True)
    assert h.ctl.view(view.id).state == "READY"


# --------------------------------------------------------------------------- the helpers
def test_resolve_anywhere_accepts_a_real_folder_and_rejects_odd_text(tmp_path: Path) -> None:
    assert resolve_anywhere(str(tmp_path)) == Path(os.path.realpath(tmp_path))
    for bad in ("", "relative", "C:\\a\\..\\b", "C:\\", "\\\\server\\share"):
        with pytest.raises(PathSafetyError):
            resolve_anywhere(bad)


def test_resolve_source_follows_the_department_setting(tmp_path: Path) -> None:
    config = AppConfig(source_roots=[str(tmp_path / "storage")])
    (tmp_path / "storage").mkdir()
    (tmp_path / "x").mkdir()
    base = Department(name="M", sender_emails=["a@dept.org"], destination_root=str(tmp_path / "d"))
    inside = str(tmp_path / "storage")
    assert resolve_source(config, base, inside) == Path(os.path.realpath(inside))
    with pytest.raises(PathSafetyError):
        resolve_source(config, base, str(tmp_path / "x"))
    open_dept = base.model_copy(update={"browse_anywhere": True})
    assert resolve_source(config, open_dept, str(tmp_path / "x")) == Path(
        os.path.realpath(tmp_path / "x")
    )


# --------------------------------------------------------------------------- the settings
def test_the_option_is_off_by_default_and_old_settings_still_load() -> None:
    department = Department(name="M", sender_emails=["a@dept.org"], destination_root="D:\\Out")
    assert department.browse_anywhere is False
    assert AppConfig.model_validate({"schema_version": 1}).departments == []


def test_the_department_dialog_saves_the_option(qtbot: Any) -> None:
    dialog = DepartmentDialog(None)
    qtbot.addWidget(dialog)
    assert not dialog.anywhere.isChecked()
    dialog.name.setText("Media")
    dialog.emails.setPlainText("a@dept.org")
    dialog.root.setText("D:\\Out")
    dialog.anywhere.setChecked(True)
    dialog._accept()
    assert dialog.result_department is not None and dialog.result_department.browse_anywhere
    again = DepartmentDialog(dialog.result_department)
    qtbot.addWidget(again)
    assert again.anywhere.isChecked()


def test_the_wizard_and_the_table_know_about_the_option(qtbot: Any, rig: Any) -> None:  # noqa: F811
    wizard = FirstRunWizard(AppConfig())
    qtbot.addWidget(wizard)
    wizard.folders.add("D:\\Programs")
    wizard.dept_name.setText("Media")
    wizard.dept_emails.setPlainText("a@dept.org")
    wizard.dept_root.setText("D:\\Out")
    assert wizard.build_config().departments[0].browse_anywhere is False
    wizard.anywhere.setChecked(True)
    assert wizard.build_config().departments[0].browse_anywhere is True

    page = rig.window.settings
    page.departments[0] = page.departments[0].model_copy(update={"browse_anywhere": True})
    page._fill_departments()
    assert "from anywhere" in page.dept_table.item(0, 3).text()


def test_the_dialog_says_where_a_pick_may_be(
    h: Harness,  # noqa: F811
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    view = _review(h)
    folder = str(_elsewhere(tmp_path)).replace("\\", "/")
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: folder))
    dialog = CandidateDialog(h.ctl, view)
    qtbot.addWidget(dialog)
    dialog.browse_folder.click()
    assert "inside a storage folder" in dialog.hint.text()
    _anywhere(h)
    other = CandidateDialog(h.ctl, view)
    qtbot.addWidget(other)
    other.browse_folder.click()
    assert "anywhere" in other.hint.text()

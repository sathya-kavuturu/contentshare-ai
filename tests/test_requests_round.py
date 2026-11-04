"""One request per email, choosing content by browsing, and the optional checksum file."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QFileDialog

from contentshare.app.controller import ActionError
from contentshare.config.models import AppConfig
from contentshare.jobs.states import RequestState as S
from contentshare.parsing.dates import find_dates, same_days
from contentshare.parsing.request import parse_request
from contentshare.ui.dialogs import CandidateDialog
from test_controller import Harness, h  # noqa: F401  (the controller fixture)
from test_feedback_round import rig  # noqa: F401  (the window fixture)

TODAY = date(2026, 11, 4)


# --------------------------------------------------------------------------- one mail, one request
@pytest.mark.parametrize(
    ("subject", "body"),
    [
        ("Silver Bay 12 Oct", "Please share the Silver Bay of 12th October 2025."),
        ("Silver Bay 12 Oct 2025", "Please share the Silver Bay of 12 Oct."),
        ("Satsang 12 Oct", "Please share the Satsang held on October 12, 2025."),
        ("Satsang", "Please share the Satsang of 12 Oct and 12 Oct 2025."),
    ],
)
def test_the_same_day_written_with_and_without_a_year_is_one_request(
    subject: str, body: str
) -> None:
    items = parse_request(subject, body, today=TODAY).items
    assert len(items) == 1
    assert items[0].dates[0].start == date(2025, 10, 12)  # the year that was written wins


def test_two_different_days_are_still_two_requests() -> None:
    parsed = parse_request("Silver Bay", "Please share 12 Oct 2025 and 13 Oct 2025.", today=TODAY)
    assert len(parsed.items) == 2
    parsed = parse_request("Silver Bay 12 Oct", "Also the one on 13 October 2025.", today=TODAY)
    assert len(parsed.items) == 2


def test_the_same_day_with_two_different_years_stays_two_requests() -> None:
    items = parse_request("Silver Bay", "Share 12 Oct 2025 and 12 Oct 2024.", today=TODAY).items
    assert len(items) == 2


def test_dates_without_a_year_are_marked_as_guessed() -> None:
    (plain,) = find_dates("12 Oct", today=TODAY)
    (stated,) = find_dates("12 Oct 2025", today=TODAY)
    (month_first,) = find_dates("Oct 12", today=TODAY)
    (ranged,) = find_dates("12-14 Oct", today=TODAY)
    assert plain.year_guessed and month_first.year_guessed and ranged.year_guessed
    assert not stated.year_guessed
    assert same_days([plain, stated]) == [stated]


def test_one_email_gives_one_request_through_the_whole_check(h: Harness) -> None:  # noqa: F811
    h.mail(
        message_id="dup1",
        subject="Sunrise Talk 19 Oct",
        body="Please share the Sunrise Talk recording of 19th October 2026.\nThanks",
    )
    views = h.ctl.views()
    assert len(views) == 1, [(v.id, v.requested) for v in views]


# --------------------------------------------------------------------------- browse and choose
def _needs_review(h: Harness) -> Any:  # noqa: F811
    h.mail(message_id="b1", subject="Quantum Lecture", body="need it")
    (view,) = h.ctl.views([S.NEEDS_REVIEW])
    return view


def test_a_browsed_folder_inside_storage_can_be_chosen(h: Harness) -> None:  # noqa: F811
    view = _needs_review(h)
    folder = h.world.storage / "2025" / "AnnualDay_2025-12-25"
    h.ctl.choose_path(view.id, str(folder))
    after = h.ctl.view(view.id)
    assert after.state == "READY"
    assert after.source_path and after.source_path.endswith("AnnualDay_2025-12-25")
    assert after.file_count == 3 and (after.destination_path or "").startswith(str(h.world.media))
    assert any(e["event"] == "state_changed" for e in h.ctl.audit(view.id))


def test_a_browsed_single_file_can_be_chosen(h: Harness) -> None:  # noqa: F811
    view = _needs_review(h)
    file = h.world.storage / "2025" / "AnnualDay_2025-12-25" / "video" / "annual.mp4"
    h.ctl.choose_path(view.id, str(file))
    after = h.ctl.view(view.id)
    assert after.state == "READY" and after.file_count == 1 and after.total_bytes == 900


def test_content_added_after_the_last_scan_can_be_chosen(h: Harness) -> None:  # noqa: F811
    view = _needs_review(h)
    new = h.world.storage / "2026" / "BrandNew_2026-11-01"
    new.mkdir(parents=True)
    (new / "talk.mp4").write_bytes(b"x" * 50)
    assert h.world.index.find_path(str(h.world.storage), "2026/BrandNew_2026-11-01") is None
    h.ctl.choose_path(view.id, str(new))
    after = h.ctl.view(view.id)
    assert after.state == "READY" and after.file_count == 1 and after.total_bytes == 50
    assert (after.destination_path or "").endswith("BrandNew_2026-11-01")


def test_browsing_outside_the_storage_folders_is_refused(
    h: Harness,  # noqa: F811
    tmp_path: Path,
) -> None:
    view = _needs_review(h)
    elsewhere = tmp_path / "somewhere_else"
    elsewhere.mkdir()
    with pytest.raises(ActionError, match="outside the storage folders"):
        h.ctl.choose_path(view.id, str(elsewhere))
    with pytest.raises(ActionError, match="outside the storage folders"):
        h.ctl.choose_path(view.id, str(h.world.storage / ".." / "media_incoming"))
    assert h.ctl.view(view.id).state == "NEEDS_REVIEW"


def test_the_whole_storage_folder_cannot_be_chosen(h: Harness) -> None:  # noqa: F811
    view = _needs_review(h)
    with pytest.raises(ActionError, match="not the storage folder itself"):
        h.ctl.choose_path(view.id, str(h.world.storage))
    with pytest.raises(ActionError, match="not the storage folder itself"):
        h.ctl.choose_path(view.id, str(h.world.storage) + "\\")


def test_browsing_a_path_that_does_not_exist_is_refused(h: Harness) -> None:  # noqa: F811
    view = _needs_review(h)
    with pytest.raises(ActionError, match="not on the storage server"):
        h.ctl.choose_path(view.id, str(h.world.storage / "2025" / "Nothing_Here"))


def test_browsing_is_refused_once_the_request_is_past_review(h: Harness) -> None:  # noqa: F811
    v = h.ready()
    h.ctl.approve(v.id, confirmed=True)
    h.wait_copy(v.id)
    with pytest.raises(ActionError, match="can no longer be changed"):
        h.ctl.choose_path(v.id, str(h.world.storage / "2025" / "AnnualDay_2025-12-25"))


def test_the_dialog_browses_for_a_folder(
    h: Harness,  # noqa: F811
    qtbot: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    view = _needs_review(h)
    starts: list[Any] = []
    folder = str(h.world.storage / "2025" / "AnnualDay_2025-12-25").replace("\\", "/")

    def pick_folder(*args: Any, **_kw: Any) -> str:
        starts.append(args[2])
        return folder

    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(pick_folder))
    dialog = CandidateDialog(h.ctl, view)
    qtbot.addWidget(dialog)
    dialog.browse_folder.click()
    assert starts == [h.ctl.config.source_roots[0]]  # opens at the storage server
    assert "You chose" in dialog.results.item(0).text()
    dialog.query.setText("Annual Day 2025")
    dialog._search()  # a later search keeps the pick at the top
    assert "You chose" in dialog.results.item(0).text()
    dialog.results.setCurrentRow(0)
    dialog.accept()
    assert dialog.chosen is None and dialog.chosen_path == folder.replace("/", "\\")


def test_the_dialog_can_browse_for_a_file(
    h: Harness,  # noqa: F811
    qtbot: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    view = _needs_review(h)
    file = str(h.world.storage / "2025" / "AnnualDay_2025-12-25" / "video" / "annual.mp4")
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (file.replace("\\", "/"), ""))
    )
    dialog = CandidateDialog(h.ctl, view)
    qtbot.addWidget(dialog)
    dialog.browse_file.click()
    dialog.results.setCurrentRow(0)
    dialog.accept()
    assert dialog.chosen_path == file


def test_cancelling_the_browse_adds_nothing(
    h: Harness,  # noqa: F811
    qtbot: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    view = _needs_review(h)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: ("", "")))
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: ""))
    dialog = CandidateDialog(h.ctl, view)
    qtbot.addWidget(dialog)
    before = dialog.results.count()
    dialog.browse_file.click()
    dialog.browse_folder.click()
    assert dialog.results.count() == before and dialog.chosen_path is None


# --------------------------------------------------------------------------- checksum file
def test_the_checksum_file_is_off_by_default_and_can_be_switched_on() -> None:
    assert AppConfig().copy_options.write_checksum_file is False
    assert AppConfig.model_validate({"schema_version": 1}).copy_options.write_checksum_file is False
    on = AppConfig.model_validate({"copy_options": {"write_checksum_file": True}})
    assert on.copy_options.write_checksum_file


def _checksum_files(folder: Path) -> list[Path]:
    return [p for p in folder.rglob("*") if ".checksums." in p.name]


def test_a_copy_leaves_no_checksum_file_by_default(h: Harness) -> None:  # noqa: F811
    v = h.ready()
    h.ctl.approve(v.id, confirmed=True)
    h.wait_copy(v.id)
    assert h.ctl.view(v.id).state == "VERIFIED"
    copied = Path(v.destination_path or "")
    assert (copied / "video.mxf").exists() and _checksum_files(copied) == []


def test_a_copy_keeps_the_checksum_file_when_asked(h: Harness) -> None:  # noqa: F811
    on = h.ctl.config.copy_options.model_copy(update={"write_checksum_file": True})
    h.ctl.apply_config(h.ctl.config.model_copy(update={"copy_options": on}), persist=False)
    v = h.ready()
    h.ctl.approve(v.id, confirmed=True)
    h.wait_copy(v.id)
    assert len(_checksum_files(Path(v.destination_path or ""))) == 1


def test_settings_show_and_save_the_checksum_option(rig: Any) -> None:  # noqa: F811
    page = rig.window.settings
    assert not page.keep_checksums.isChecked()
    page.keep_checksums.setChecked(True)
    assert page.collect().copy_options.write_checksum_file

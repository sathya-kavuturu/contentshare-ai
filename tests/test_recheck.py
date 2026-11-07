"""Refresh and "Ask the model" on a request, and the question about unsaved settings."""

from __future__ import annotations

from typing import Any

import pytest
from PySide6.QtWidgets import QPushButton

from contentshare.app import controller as controller_module
from contentshare.app.controller import ActionError
from contentshare.smart.runtime import SmartError
from contentshare.smart.schema import Reading
from contentshare.ui.main_window import PAGES
from test_controller import Harness, h  # noqa: F401  (the controller fixture)
from test_ui import Rig, rig  # noqa: F401  (the window fixture)

WANTED = "Orchid Lake Session"


def _exact(h: Harness) -> None:  # noqa: F811
    departments = [
        d.model_copy(update={"exact_folder_names": True}) for d in h.ctl.config.departments
    ]
    h.ctl.apply_config(h.ctl.config.model_copy(update={"departments": departments}), persist=False)


def _make_folder(h: Harness, name: str = WANTED) -> None:  # noqa: F811
    folder = h.world.storage / "Weekly" / name
    folder.mkdir(parents=True)
    (folder / "a.mp4").write_bytes(b"x" * 10)


def _waiting_email(h: Harness) -> int:  # noqa: F811
    _exact(h)
    h.mail(message_id="r1", subject="Content request", body=f"Hello\n- {WANTED}\nThanks")
    (view,) = h.ctl.views()
    assert view.state == "NEEDS_REVIEW" and "No folder named" in view.reason
    return view.email_id


# --------------------------------------------------------------------------- Refresh
def test_refresh_finds_a_folder_that_was_made_after_the_email_was_read(
    h: Harness,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    email_id = _waiting_email(h)
    assert "nothing new" in h.ctl.refresh_email(email_id)  # not there yet
    _make_folder(h)
    monkeypatch.setattr(controller_module, "RECHECK_MINUTES", 0)  # read the storage again now
    message = h.ctl.refresh_email(email_id)
    assert "1 of 1 folders found" in message and message.startswith("Read the storage folders")
    (view,) = h.ctl.views()
    assert view.state == "READY" and (view.source_path or "").endswith(WANTED)
    assert h.ctl.refresh_email(email_id).startswith("Nothing to look up")  # nothing waits now


def test_refresh_leaves_a_folder_somebody_chose_alone(h: Harness) -> None:  # noqa: F811
    email_id = _waiting_email(h)
    _make_folder(h, "Chosen By Hand")
    h.world.index.scan_all([str(h.world.storage)])
    (view,) = h.ctl.views()
    (hit,) = [c for c in h.ctl.search(view.id, "Chosen By Hand") if c.item_id is not None][:1]
    h.ctl.choose(view.id, hit.item_id)  # type: ignore[arg-type]
    chosen = h.ctl.view(view.id).source_path
    _make_folder(h)
    h.world.index.scan_all([str(h.world.storage)])
    assert h.ctl.refresh_email(email_id).startswith("Nothing to look up")
    assert h.ctl.view(view.id).source_path == chosen


def test_refresh_does_not_guess_for_a_department_that_gives_exact_names(
    h: Harness,  # noqa: F811
) -> None:
    email_id = _waiting_email(h)
    _make_folder(h, "Orchid Lake Session Extra")  # similar, but not the name
    h.world.index.scan_all([str(h.world.storage)])
    h.ctl.refresh_email(email_id)
    assert h.ctl.views()[0].state == "NEEDS_REVIEW"


# --------------------------------------------------------------------------- Ask the model
class _Reader:
    def __init__(self, reading: Reading | Exception) -> None:
        self.reading = reading
        self.calls = 0

    def __call__(self, *_args: Any) -> _Reader:
        return self

    def read(self, _subject: str, _body: str) -> Reading:
        self.calls += 1
        if isinstance(self.reading, Exception):
            raise self.reading
        return self.reading


def test_ask_the_model_adds_the_folders_it_names_and_logs_the_time(
    h: Harness,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    email_id = _waiting_email(h)
    _make_folder(h, "Model Found Talk")
    h.world.index.scan_all([str(h.world.storage)])
    reader = _Reader(Reading(True, (WANTED, "Model Found Talk"), 0.9, seconds=1.5))
    monkeypatch.setattr(controller_module, "SmartReader", reader)
    message = h.ctl.ask_model_about_email(email_id)
    assert "found 1 new folder" in message and "1.5 s" in message  # WANTED was listed already
    items = h.ctl.group(email_id).items
    assert [v.requested for v in items] == [WANTED, "Model Found Talk"]
    assert items[1].state == "NEEDS_REVIEW" or items[1].state == "READY"
    logged = [e for e in h.ctl.audit(items[0].id) if e["event"] == "model_read"]
    assert logged and "answered in 1.5 s" in logged[0]["detail"]
    assert "no folder that is not listed" in h.ctl.ask_model_about_email(email_id)


def test_ask_the_model_says_so_when_it_is_not_a_request_or_cannot_answer(
    h: Harness,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    email_id = _waiting_email(h)
    monkeypatch.setattr(controller_module, "SmartReader", _Reader(Reading(False, seconds=0.8)))
    assert "not a content request" in h.ctl.ask_model_about_email(email_id)
    assert len(h.ctl.group(email_id).items) == 1
    monkeypatch.setattr(controller_module, "SmartReader", _Reader(SmartError("not installed")))
    with pytest.raises(ActionError, match="not installed"):
        h.ctl.ask_model_about_email(email_id)


# --------------------------------------------------------------------------- on the card
def test_the_card_has_refresh_and_ask_buttons_that_call_the_controller(
    rig: Rig,  # noqa: F811
    qtbot: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig.mail(message_id="c1", subject="Content request", body="Please share the English recording")
    rig.window.requests.refresh()
    (card,) = rig.cards()
    texts = [b.text() for b in card.findChildren(QPushButton)]
    assert "↻" in texts and "Ask model" in texts
    calls: list[str] = []
    monkeypatch.setattr(
        rig.controller, "refresh_email", lambda _i: calls.append("r") or "Looked again."
    )
    monkeypatch.setattr(
        rig.controller, "ask_model_about_email", lambda _i: calls.append("a") or "Done."
    )
    card.refresh_button.click()
    qtbot.waitUntil(lambda: "r" in calls)
    rig.cards()[0].ask_button.click()  # the list may have been rebuilt meanwhile
    qtbot.waitUntil(lambda: "a" in calls)


# --------------------------------------------------------------------------- unsaved settings
def _go(rig: Rig, name: str) -> None:  # noqa: F811
    from contentshare.ui.main_window import PAGES

    rig.window.nav.setCurrentRow(PAGES.index(name))


def test_settings_that_are_just_loaded_have_nothing_unsaved(rig: Rig) -> None:  # noqa: F811
    _go(rig, "Settings")
    assert not rig.window.settings.has_unsaved()
    rig.window.settings.dry_run.setChecked(not rig.window.settings.dry_run.isChecked())
    assert rig.window.settings.has_unsaved()
    rig.window.settings.load()
    assert not rig.window.settings.has_unsaved()


def test_leaving_settings_with_changes_asks_and_stay_keeps_you_there(
    rig: Rig,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contentshare.ui import settings_page

    asked: list[int] = []
    monkeypatch.setattr(settings_page, "ask_unsaved", lambda _p: asked.append(1) or "stay")
    _go(rig, "Settings")
    page = rig.window.settings
    page.dry_run.setChecked(not page.dry_run.isChecked())
    _go(rig, "History")
    assert asked == [1]
    assert rig.window.stack.currentWidget() is page
    assert rig.window.nav.currentRow() == PAGES.index("Settings")
    assert page.has_unsaved()  # the edit is still there


def test_leaving_settings_can_save_or_drop_the_changes(
    rig: Rig,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contentshare.ui import settings_page

    page = rig.window.settings
    saved_before = rig.controller.config.dry_run
    answers = iter(["discard", "save"])
    monkeypatch.setattr(settings_page, "ask_unsaved", lambda _p: next(answers))
    _go(rig, "Settings")
    page.dry_run.setChecked(not saved_before)
    _go(rig, "History")  # "Don't save"
    assert rig.window.stack.currentWidget() is rig.window.history
    assert (
        rig.controller.config.dry_run == saved_before and page.dry_run.isChecked() == saved_before
    )
    _go(rig, "Settings")
    page.dry_run.setChecked(not saved_before)
    _go(rig, "Requests")  # "Save changes"
    assert rig.window.stack.currentIndex() == 0
    assert rig.controller.config.dry_run == (not saved_before)


def test_leaving_settings_without_changes_does_not_ask(
    rig: Rig,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contentshare.ui import settings_page

    monkeypatch.setattr(settings_page, "ask_unsaved", lambda _p: pytest.fail("asked"))
    _go(rig, "Settings")
    _go(rig, "History")
    assert rig.window.stack.currentWidget() is rig.window.history


def test_changes_that_cannot_be_saved_keep_you_in_settings(
    rig: Rig,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contentshare.ui import settings_page
    from contentshare.ui.widgets import SafeMessageBox

    monkeypatch.setattr(settings_page, "ask_unsaved", lambda _p: "save")
    monkeypatch.setattr(SafeMessageBox, "warning", staticmethod(lambda *_a, **_k: None))
    _go(rig, "Settings")
    page = rig.window.settings
    page.default_reply.setPlainText("")  # an empty reply text cannot be saved
    _go(rig, "History")
    assert rig.window.stack.currentWidget() is page


# --------------------------------------------------------------------------- put a skipped one back
def test_a_skipped_request_can_be_put_back(h: Harness) -> None:  # noqa: F811
    _waiting_email(h)
    (view,) = h.ctl.views()
    with pytest.raises(ActionError, match="Only a skipped"):
        h.ctl.restore(view.id)
    h.ctl.skip(view.id)
    assert h.ctl.view(view.id).state == "SKIPPED"
    h.ctl.restore(view.id)
    assert h.ctl.view(view.id).state == "NEEDS_REVIEW"  # no content chosen yet


def test_history_puts_a_skipped_request_back(rig: Rig) -> None:  # noqa: F811
    rig.mail(message_id="s1", subject="Content request", body="Please share the English recording")
    (view,) = rig.controller.views()
    rig.controller.skip(view.id)
    page = rig.window.history
    page.refresh()
    assert not page.restore_button.isEnabled()
    page.table.selectRow(0)
    assert page.restore_button.isEnabled()
    page.restore_button.click()
    assert rig.controller.view(view.id).state in {"NEEDS_REVIEW", "READY"}


# --------------------------------------------------------------------------- a long warning
def test_a_long_warning_does_not_widen_the_card(rig: Rig, qtbot: Any) -> None:  # noqa: F811
    from contentshare.ui.request_card import _chip, _note_row

    chip = _chip("⚠ " + "These lines of the email were not searched; " * 12, "warn")
    _note_row(chip)
    assert chip.wordWrap() and chip.minimumSizeHint().width() < 400


def test_settings_keep_the_copy_by_itself_choice(rig: Rig) -> None:  # noqa: F811
    _go(rig, "Settings")
    page = rig.window.settings
    assert not page.auto_copy.isChecked()
    page.auto_copy.setChecked(True)
    assert page.has_unsaved()
    page.save()
    assert rig.controller.config.auto_copy_certain

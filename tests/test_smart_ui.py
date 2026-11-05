"""Smart reading on screen: the settings tab and the request card."""

from __future__ import annotations

from typing import Any

import pytest
from PySide6.QtWidgets import QDialog, QInputDialog, QPushButton

from contentshare.config.models import SmartSettings
from contentshare.gmail.intake import IntakeService
from contentshare.smart.schema import Reading
from contentshare.ui.smart_box import SmartBox, TryDialog, describe
from fake_gmail import make_message
from helpers import TODAY
from test_ui import Rig, rig  # noqa: F401  (the window fixture)

EXISTING = "YouthRetreat_2026-09-04_Day2"


def test_the_settings_have_a_smart_reading_tab_with_safe_defaults(rig: Rig) -> None:  # noqa: F811
    page = rig.window.settings
    names = [page.tabs.tabText(i) for i in range(page.tabs.count())]
    assert "Smart reading" in names
    assert page.collect().smart == SmartSettings()  # off until the person turns it on
    page.smart_box.enabled.setChecked(True)
    page.smart_box.per_check.setValue(7)
    page.smart_box.model.setCurrentIndex(page.smart_box.model.findData("small"))
    smart = page.collect().smart
    assert smart.enabled and smart.max_per_check == 7 and smart.model == "small"
    assert (
        "Not installed" in page.smart_box.state.text() and not page.smart_box.try_button.isEnabled()
    )


def test_download_asks_to_save_first_when_the_choice_changed(
    rig: Rig,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    box: SmartBox = rig.window.settings.smart_box
    asked: list[str] = []
    monkeypatch.setattr(
        "contentshare.ui.smart_box.QMessageBox.information",
        lambda _p, title, _t: asked.append(title),
    )
    box.gpu.setChecked(True)  # a change that is not saved yet
    box.download.click()
    assert asked == ["Save first"] and not box._installing


def test_what_the_model_found_is_described_in_words() -> None:
    assert "Not a content request" in describe(Reading(False, reason="newsletter"))
    text = describe(Reading(True, ("Talk A", "Talk B"), 0.9))
    assert "• Talk A" in text and "• Talk B" in text and "90%" in text


def test_try_dialog_offers_sample_emails_and_shows_the_answer_and_time(
    rig: Rig,  # noqa: F811
    qtbot: Any,
) -> None:
    from contentshare.smart.samples import SAMPLES

    dialog = TryDialog(rig.window.ctx)
    qtbot.addWidget(dialog)
    assert dialog.template.count() == len(SAMPLES) + 1
    assert dialog.subject.text() == SAMPLES[0].subject  # the first sample is already there
    dialog.template.setCurrentIndex(3)
    assert (
        dialog.body.toPlainText() == SAMPLES[2].body and SAMPLES[2].expect in dialog.expect.text()
    )
    dialog.body.setPlainText(dialog.body.toPlainText() + "\nAlso a changed line")  # editable
    seen: list[str] = []
    rig.controller.smart_test = lambda _s, b: (  # type: ignore[method-assign]
        seen.append(b)
        or Reading(True, ("Talk A",), 0.9, seconds=2.34, answer='{"is_request": true}')
    )
    dialog.go.click()
    qtbot.waitUntil(lambda: "answered in 2.3 s" in dialog.outcome.text())
    assert "changed line" in seen[0] and dialog.answer.toPlainText() == '{"is_request": true}'
    dialog.template.setCurrentIndex(0)
    assert dialog.subject.text() == "" and dialog.body.toPlainText() == ""


def test_try_dialog_says_when_the_model_was_not_asked(
    rig: Rig,  # noqa: F811
    qtbot: Any,
) -> None:
    dialog = TryDialog(rig.window.ctx)
    qtbot.addWidget(dialog)
    rig.controller.smart_test = lambda _s, _b: Reading(False, reason="no sign of a request")  # type: ignore[method-assign]
    dialog.go.click()
    qtbot.waitUntil(lambda: "was not asked" in dialog.outcome.text())


def test_try_dialog_shows_the_answer_or_the_reason_it_failed(
    rig: Rig,  # noqa: F811
    qtbot: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialog = TryDialog(rig.window.ctx)
    qtbot.addWidget(dialog)
    dialog.subject.setText("Request")
    dialog.body.setPlainText("Kindly share Talk A")
    rig.controller.smart_test = lambda _s, _b: Reading(True, ("Talk A",), 1.0)  # type: ignore[method-assign]
    dialog.go.click()
    qtbot.waitUntil(lambda: "Talk A" in dialog.outcome.text())
    assert dialog.go.isEnabled()
    rig.controller.smart_test = lambda _s, _b: (_ for _ in ()).throw(  # type: ignore[method-assign]
        __import__("contentshare.app.controller", fromlist=["ActionError"]).ActionError(
            "not installed"
        )
    )
    dialog.go.click()
    qtbot.waitUntil(lambda: "not installed" in dialog.outcome.text())


def test_a_request_from_an_unlisted_sender_offers_choose_department(
    rig: Rig,  # noqa: F811
    qtbot: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig.world.gmail.add(
        make_message(
            message_id="s1",
            from_="Ravi <ravi@elsewhere.com>",
            subject="Please share",
            body=EXISTING,
        )
    )
    config = rig.controller.config.model_copy(update={"smart": SmartSettings(enabled=True)})
    IntakeService(
        rig.world.gateway, rig.world.repo, rig.world.index, config, today=lambda: TODAY
    ).check_now()
    rig.window.requests.refresh()
    (card,) = rig.cards()
    labels = [b.text() for b in card.findChildren(QPushButton)]
    assert "Choose department…" in labels and "Add a folder…" in labels
    monkeypatch.setattr(QInputDialog, "getItem", lambda *_a, **_k: ("Media Team", True))
    card._choose_department()
    assert rig.controller.views()[0].department == "Media Team"
    qtbot.wait(300)
    (card,) = rig.cards()
    assert "Choose department…" not in [b.text() for b in card.findChildren(QPushButton)]
    assert QDialog is not None


def test_the_prompt_shown_in_settings_is_what_is_sent_to_the_model() -> None:
    from contentshare.smart.prompt import SYSTEM
    from contentshare.ui.smart_box import prompt_text

    text = prompt_text()
    assert SYSTEM in text and "<email>" in text and "is_request" in text

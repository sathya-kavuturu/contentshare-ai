"""Screens, driven through real widgets (offscreen). Logic is covered in test_controller.py;
these check that the screens show it, enforce the rules, and never block or crash."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication, QCheckBox, QDialog, QPushButton

from contentshare.app.controller import Controller
from contentshare.app.telegram import Chat, TelegramError, valid_token
from contentshare.config.store import load_config
from contentshare.storage.db import Database
from contentshare.ui.bridge import Bridge
from contentshare.ui.copies_page import progress_text, stage_line
from contentshare.ui.dialogs import ReplyDialog
from contentshare.ui.history_page import audit_text, matches
from contentshare.ui.main_window import MainWindow
from contentshare.ui.request_card import RequestCard, gmail_url
from contentshare.ui.requests_page import summarize_check
from contentshare.ui.settings_page import SettingsPage, lines
from contentshare.ui.status import confidence_dots, human_eta, style_for
from contentshare.ui.theme import DARK, LIGHT, apply_theme
from contentshare.ui.widgets import SafeMessageBox as QMessageBox
from contentshare.ui.wizard import FirstRunWizard
from fake_gmail import auth_header, make_message
from helpers import TODAY, MailWorld, build_mail_world


class Boxes:
    """Records message boxes instead of showing them (a modal box would block the test)."""

    def __init__(self) -> None:
        self.shown: list[tuple[str, str, str]] = []
        self.answer = QMessageBox.StandardButton.Yes

    def _record(self, kind: str, args: tuple[Any, ...]) -> None:
        self.shown.append((kind, str(args[1]), str(args[2]) if len(args) > 2 else ""))

    def last(self) -> str:
        return self.shown[-1][2]


@pytest.fixture
def boxes(monkeypatch: pytest.MonkeyPatch) -> Boxes:
    b = Boxes()
    for name in ("warning", "information", "critical"):
        monkeypatch.setattr(
            QMessageBox, name, staticmethod(lambda *a, n=name, **k: b._record(n, a))
        )

    def question(*args: Any, **kwargs: Any) -> QMessageBox.StandardButton:
        b._record("question", args)
        return b.answer

    monkeypatch.setattr(QMessageBox, "question", staticmethod(question))
    return b


class Rig:
    def __init__(self, world: MailWorld, tmp_path: Path) -> None:
        self.world = world
        self.bridge = Bridge()
        self.controller = Controller(
            world.repo, world.index, world.config, gateway_factory=lambda: world.gateway,
            config_path=tmp_path / "config.yaml", today=lambda: TODAY,
        )  # fmt: skip
        self.controller.subscribe(self.bridge.forward)
        self.window = MainWindow(self.controller, self.bridge)

    def mail(self, **kwargs: Any) -> None:
        self.world.gmail.add(make_message(**kwargs))
        self.controller.check_now()
        self.window.requests.refresh()

    def ready_mail(self) -> None:
        self.mail(
            message_id="r1", subject="Sunrise Talk 12 Oct 2026", body="English recording please"
        )

    def cards(self) -> list[RequestCard]:
        return list(self.window.requests._cards)

    def click(self, card: RequestCard, text: str) -> None:
        button = next(b for b in card.findChildren(QPushButton) if b.text().startswith(text))
        button.click()


@pytest.fixture
def rig(qtbot: Any, tmp_path: Path, db: Database) -> Iterator[Rig]:
    apply_theme(QApplication.instance())  # type: ignore[arg-type]
    r = Rig(build_mail_world(tmp_path, db), tmp_path)
    qtbot.addWidget(r.window)
    r.window.show()
    yield r
    r.controller.shutdown()
    r.bridge.close()


# --------------------------------------------------------------------------- pure helpers
def test_status_styles_use_icon_word_and_tone() -> None:
    for state in ("NEEDS_REVIEW", "READY", "FAILED", "VERIFIED", "REPLIED", "COPYING"):
        s = style_for(state)
        assert s.icon and s.word and s.tone
    assert style_for("READY").text == "✔ Ready"
    assert style_for("WEIRD").word == "Weird"


def test_confidence_and_eta_text() -> None:
    assert confidence_dots(92) == "●●●●● 92%"
    assert confidence_dots(41) == "●●○○○ 41%"
    assert confidence_dots(None) == "not scored"
    assert human_eta(None) == "calculating…"
    assert human_eta(42) == "42 s"
    assert human_eta(112) == "1 min 52 s"
    assert human_eta(7500) == "2 h 05 min"


def test_theme_tones_meet_contrast_in_both_variants() -> None:
    def lum(hex_color: str) -> float:
        rgb = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    def ratio(a: str, b: str) -> float:
        hi, lo = sorted((lum(a), lum(b)), reverse=True)
        return (hi + 0.05) / (lo + 0.05)

    for theme in (LIGHT, DARK):
        for name, tone in theme.tones.items():
            assert ratio(tone.fg, tone.bg) >= 4.5, (theme.name, name)
        assert ratio(theme.text, theme.card) >= 7
        assert ratio(theme.muted, theme.card) >= 4.5
        assert ratio(theme.accent_text, theme.accent) >= 4.5
        assert ratio(theme.banner.fg, theme.banner.bg) >= 4.5


def test_helpers_gmail_url_lines_matches_audit_text() -> None:
    assert gmail_url("19b4f0c2a1d3e5f7") is not None
    assert gmail_url("../../evil") is None
    assert gmail_url("") is None
    assert lines("a@x.org, b@x.org\n\n c@x.org ") == ["a@x.org", "b@x.org", "c@x.org"]
    text = audit_text(
        [{"ts": "2026-11-03T09:00:00+00:00", "actor": "user", "event": "state_changed",
          "detail": '{"to": "READY", "reason": "ok"}'}]
    )  # fmt: skip
    assert "READY" in text and "ok" in text
    assert audit_text([{"ts": "t", "actor": "a", "event": "e", "detail": "{bad"}])
    assert stage_line("verify") == "Copy ▸ [Verify] ▸ Finalise"


def test_summarize_check_wording() -> None:
    from contentshare.gmail.intake import IntakeReport

    assert "not connected" in summarize_check(IntakeReport(needs_sign_in=True))
    text = summarize_check(IntakeReport(examined=3, requests=4, ready=1, needs_review=3, ignored=2))
    assert "3 new email" in text and "1 ready, 3 to review" in text and "2 ignored" in text


# --------------------------------------------------------------------------- requests screen
def test_empty_state_and_navigation(rig: Rig) -> None:
    page = rig.window.requests
    assert page.empty.isVisible() and "Check now" in page.empty.text()
    assert page.last_check.text() == "Never checked"
    assert not rig.window.dry_banner.isVisible()
    assert rig.window.gmail_banner.isVisible()  # no auth object in this rig
    for row in range(4):
        rig.window.nav.setCurrentRow(row)
        assert rig.window.stack.currentIndex() == row


def test_empty_index_banner(rig: Rig) -> None:
    rig.controller.apply_config(rig.controller.config.model_copy(update={"source_roots": []}),
                                persist=False)  # fmt: skip
    rig.window.requests.refresh()
    assert rig.window.requests.index_note.isVisible()


def test_ready_card_shows_everything_the_wireframe_promises(rig: Rig) -> None:
    rig.ready_mail()
    (card,) = rig.cards()
    texts = " ".join(label.text() for label in card.findChildren(type(card.status)))
    assert "✔ Ready" in texts and "Sender verified" in texts
    assert "Anita R." in texts and "Media Team" in texts
    assert "SunriseTalk_EN" in texts and "confidence" in texts
    assert "copy to" in texts.lower()  # the small heading above the destination
    assert [b.text() for b in card.findChildren(QPushButton) if b.text()][:3] == [
        "▾",  # the arrow that folds the card
        "▸ Show text",
        "Change content…",
    ]
    assert card.confirm.text() == "Confirm copy"
    assert not card.confirm.isChecked()
    assert rig.window.nav.item(0).text() == "Requests (1)"


def test_approve_without_confirm_is_refused_with_a_message(
    rig: Rig, boxes: Boxes, qtbot: Any
) -> None:
    rig.ready_mail()
    (card,) = rig.cards()
    rig.click(card, "Approve and copy")
    qtbot.waitUntil(lambda: bool(boxes.shown), timeout=5000)
    assert "Confirm copy" in boxes.last()
    assert rig.controller.views()[0].state == "READY"


def test_confirm_then_approve_copies_and_updates_the_screen(
    rig: Rig, boxes: Boxes, qtbot: Any
) -> None:
    rig.ready_mail()
    card = rig.cards()[0]
    card.confirm.setChecked(True)
    rig.click(card, "Approve and copy")
    qtbot.waitUntil(lambda: rig.controller.views()[0].state == "VERIFIED", timeout=20000)
    qtbot.waitUntil(lambda: any("✔✔" in c.status.text() for c in rig.cards()), timeout=5000)
    card = rig.cards()[0]
    assert "Preview and send reply…" in [b.text() for b in card.findChildren(QPushButton)]
    assert not boxes.shown


def test_confirm_tick_survives_a_refresh(rig: Rig) -> None:
    rig.ready_mail()
    rig.cards()[0].confirm.setChecked(True)
    rig.window.requests.refresh()
    assert rig.cards()[0].confirm.isChecked()


def test_unverified_sender_card_and_acknowledgement(rig: Rig, boxes: Boxes, qtbot: Any) -> None:
    rig.mail(
        message_id="u1", from_="Boss <editor@dept.org>", subject="Sunrise Talk 12 Oct 2026",
        auth=auth_header("dept.org", dmarc="fail", spf="fail", dkim="fail"),
    )  # fmt: skip
    (card,) = rig.cards()
    assert "NOT verified" in " ".join(lb.text() for lb in card.findChildren(type(card.status)))
    use = next(b for b in card.findChildren(QPushButton) if b.text() == "Use this")
    use.click()
    qtbot.waitUntil(lambda: rig.controller.views()[0].state == "READY", timeout=5000)
    rig.window.requests.refresh()
    card = rig.cards()[0]
    assert card.ack is not None and not card.ack.isChecked()
    card.confirm.setChecked(True)
    rig.click(card, "Approve and copy")
    qtbot.waitUntil(lambda: bool(boxes.shown), timeout=5000)
    assert "could not be verified" in boxes.last()


def test_review_card_lists_candidates_and_use_this_moves_to_ready(rig: Rig, qtbot: Any) -> None:
    rig.mail(
        message_id="v1", from_="Boss <editor@dept.org>", subject="Sunrise Talk",
        body="12 Oct 2026 english",
        auth=auth_header("dept.org", dmarc="fail", spf="fail", dkim="fail"),
    )  # fmt: skip
    (card,) = rig.cards()
    assert "Needs review" in card.status.text()
    use_buttons = [b for b in card.findChildren(QPushButton) if b.text() == "Use this"]
    assert 1 <= len(use_buttons) <= 5
    use_buttons[0].click()
    qtbot.waitUntil(lambda: rig.controller.views()[0].state == "READY", timeout=5000)


def test_check_now_button_reads_mail_in_the_background(rig: Rig, qtbot: Any) -> None:
    rig.world.gmail.add(make_message(message_id="c1", subject="Sunrise Talk 12 Oct 2026"))
    page = rig.window.requests
    page.check_button.click()
    assert not page.check_button.isEnabled()
    qtbot.waitUntil(page.check_button.isEnabled, timeout=10000)
    assert len(rig.cards()) == 1
    assert "Checked mail" in rig.window.statusBar().currentMessage()
    assert page.last_check.text().startswith("Last check")


def test_check_now_when_gmail_not_connected(rig: Rig, boxes: Boxes, qtbot: Any) -> None:
    from contentshare.gmail.errors import AuthRequiredError

    def refuse() -> None:
        raise AuthRequiredError("x")

    rig.controller._gateway_factory = refuse  # type: ignore[assignment]
    rig.window.requests.check_button.click()
    qtbot.waitUntil(lambda: bool(boxes.shown), timeout=10000)
    assert "not connected" in boxes.last()


def test_filter_and_bulk_actions(rig: Rig, boxes: Boxes, qtbot: Any) -> None:
    rig.ready_mail()
    rig.mail(message_id="r2", subject="Sunrise Talk 19 Oct 2026", body="english")
    page = rig.window.requests
    assert len(rig.cards()) == 2
    page.filter.setCurrentIndex(1)  # needs review only
    assert all("Needs review" in c.status.text() for c in rig.cards())
    page.filter.setCurrentIndex(2)  # ready
    assert all("Ready" in c.status.text() for c in rig.cards())
    page.filter.setCurrentIndex(0)
    assert not page.bulk_approve.isEnabled()
    page.select_all.setChecked(True)
    assert page.bulk_approve.isEnabled() and "Approve selected" in page.bulk_approve.text()
    page.bulk_skip.click()  # answered Yes
    assert "Skip 2" in boxes.shown[-1][2]
    assert {v.state for v in rig.controller.views()} == {"SKIPPED"}


def test_bulk_approve_copies_only_verified_selected_ready_requests(
    rig: Rig, boxes: Boxes, qtbot: Any
) -> None:
    rig.ready_mail()
    page = rig.window.requests
    page.select_all.setChecked(True)
    page.bulk_approve.click()
    assert "Copy 1 request" in boxes.shown[-1][2]
    qtbot.waitUntil(lambda: rig.controller.views()[0].state == "VERIFIED", timeout=20000)


def test_bulk_approve_refuses_when_nothing_qualifies(rig: Rig, boxes: Boxes) -> None:
    rig.mail(
        message_id="b1", from_="Boss <editor@dept.org>", subject="Sunrise Talk 12 Oct 2026",
        auth=auth_header("dept.org", dmarc="fail", spf="fail", dkim="fail"),
    )  # fmt: skip
    page = rig.window.requests
    page.select_all.setChecked(True)
    assert not page.bulk_approve.isEnabled()


def test_skip_cancelled_by_user_changes_nothing(rig: Rig, boxes: Boxes) -> None:
    rig.ready_mail()
    boxes.answer = QMessageBox.StandardButton.No
    rig.click(rig.cards()[0], "Skip")
    assert rig.controller.views()[0].state == "READY"
    boxes.answer = QMessageBox.StandardButton.Yes
    rig.click(rig.cards()[0], "Skip")
    assert rig.controller.views()[0].state == "SKIPPED"


def test_reject_dialog_flow(rig: Rig, monkeypatch: pytest.MonkeyPatch, qtbot: Any) -> None:
    from contentshare.ui import request_card as module

    rig.ready_mail()

    class FakeReject:
        def __init__(self, *_a: Any, **_k: Any) -> None: ...
        def exec(self) -> int:
            return QDialog.DialogCode.Accepted

        def result_values(self) -> tuple[str, bool, str]:
            return "not available", True, "Sorry, not available."

    monkeypatch.setattr(module, "RejectDialog", FakeReject)
    rig.click(rig.cards()[0], "Reject")
    assert rig.controller.views()[0].state == "REJECTED"
    qtbot.waitUntil(lambda: len(rig.world.gmail.sent) == 1, timeout=10000)
    assert "Sorry, not available." in str(rig.world.gmail.sent[0])


def test_reply_preview_edit_and_send(rig: Rig, monkeypatch: pytest.MonkeyPatch, qtbot: Any) -> None:
    from contentshare.ui import request_card as module

    rig.ready_mail()
    rid = rig.controller.views()[0].id
    rig.controller.approve(rid, confirmed=True)
    qtbot.waitUntil(lambda: rig.controller.views()[0].state == "VERIFIED", timeout=20000)
    rig.window.requests.refresh()

    seen: dict[str, Any] = {}

    class FakeReply(ReplyDialog):
        def __init__(self, draft: Any, **kw: Any) -> None:
            super().__init__(draft, **kw)
            seen["to"], seen["summary"] = draft.to_addr, kw["summary"]
            self.body.setPlainText("Edited text")

        def exec(self) -> int:
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(module, "ReplyDialog", FakeReply)
    rig.click(rig.cards()[0], "Preview and send reply")
    qtbot.waitUntil(lambda: rig.controller.views()[0].state == "REPLIED", timeout=10000)
    assert seen["to"] == "anita@dept.org" and "verified" in seen["summary"]
    assert "Edited text" in rig.controller.replies_for(rid)[0]["body"]


def test_reply_dialog_is_disabled_in_dry_run(qtbot: Any, rig: Rig) -> None:
    from contentshare.gmail.replies import ReplyDraft

    draft = ReplyDraft(1, "shared", "a@b.org", "Re: x", "body", "t1")
    dialog = ReplyDialog(draft, summary="s", dry_run=True)
    qtbot.addWidget(dialog)
    send = next(b for b in dialog.findChildren(QPushButton) if b.text() == "Send reply")
    assert not send.isEnabled()
    assert dialog.draft().body == "body"


def test_failed_card_shows_reason_and_retry(rig: Rig, qtbot: Any) -> None:
    rig.ready_mail()
    view = rig.controller.views()[0]
    dest = Path(view.destination_path or "")
    dest.mkdir(parents=True)
    (dest / "video.mxf").write_bytes(b"theirs")
    rig.controller.approve(view.id, confirmed=True)
    qtbot.waitUntil(lambda: rig.controller.views()[0].state == "FAILED", timeout=20000)
    rig.window.requests.refresh()
    card = rig.cards()[0]
    assert "✖ Failed" in card.status.text()
    assert any(b.text() == "Retry copy" for b in card.findChildren(QPushButton))
    assert "No “shared” reply" in " ".join(lb.text() for lb in card.findChildren(type(card.status)))


def test_dry_run_banner_and_result_dialog(rig: Rig, boxes: Boxes, qtbot: Any) -> None:
    rig.ready_mail()
    rig.controller.set_dry_run(True)
    rig.window.update_chrome()
    rig.window.requests.refresh()
    assert rig.window.dry_banner.isVisible()
    card = rig.cards()[0]
    assert any(b.text() == "Check (dry run)" for b in card.findChildren(QPushButton))
    card.confirm.setChecked(True)
    rig.click(card, "Check (dry run)")
    qtbot.waitUntil(lambda: bool(boxes.shown), timeout=10000)
    kind, title, text = boxes.shown[-1]
    assert title == "Dry run" and "3 file(s)" in text and "Nothing was copied" in text
    assert rig.controller.views()[0].state == "READY"


def test_open_email_link_is_validated(
    rig: Rig, boxes: Boxes, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    from PySide6.QtGui import QDesktopServices

    monkeypatch.setattr(
        QDesktopServices, "openUrl", staticmethod(lambda u: opened.append(u.toString()))
    )
    rig.ready_mail()
    with rig.world.repo.db.transaction() as conn:
        conn.execute("UPDATE emails SET gmail_message_id = 'not an id!'")
    rig.window.requests.refresh()
    rig.click(rig.cards()[0], "Open email in Gmail")
    assert opened == [] and boxes.shown
    with rig.world.repo.db.transaction() as conn:
        conn.execute("UPDATE emails SET gmail_message_id = '19b4f0c2a1d3e5f7'")
    rig.window.requests.refresh()
    rig.click(rig.cards()[0], "Open email in Gmail")
    assert opened == ["https://mail.google.com/mail/u/0/#all/19b4f0c2a1d3e5f7"]


# --------------------------------------------------------------------------- copies
def test_active_copies_shows_progress_and_pause(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, boxes: Boxes, qtbot: Any
) -> None:
    rig.ready_mail()
    gate = threading.Event()
    runner = rig.controller.queue._runner
    real_run = runner.run

    def slow(request_id: int, control: Any, **kw: Any) -> Any:
        gate.wait(15)
        return real_run(request_id, control, **kw)

    monkeypatch.setattr(runner, "run", slow)
    rig.controller.approve(rig.controller.views()[0].id, confirmed=True)
    rig.window.nav.setCurrentRow(1)
    page = rig.window.copies
    page.refresh()
    assert page.count() == 1 and not page.empty.isVisible()
    row = next(iter(page._rows.values()))
    assert "Copying" in row.title.text()
    row.pause.click()
    page.refresh()
    assert row.pause.text() == "Resume" and "Paused" in row.title.text()
    row.pause.click()
    page.refresh()
    assert row.pause.text() == "Pause"
    rig.window.update_chrome()
    assert rig.window.nav.item(1).text() == "Active copies (1)"
    cancelled: list[int] = []
    real_cancel = rig.controller.cancel
    monkeypatch.setattr(rig.controller, "cancel", cancelled.append)
    boxes.answer = QMessageBox.StandardButton.No
    row.cancel.click()  # asks first; "No" leaves the copy running
    assert boxes.shown[-1][0] == "question" and "Stop copying" in boxes.last()
    assert cancelled == []
    boxes.answer = QMessageBox.StandardButton.Yes
    monkeypatch.setattr(rig.controller, "cancel", real_cancel)
    row.cancel.click()
    gate.set()
    qtbot.waitUntil(lambda: not rig.controller.busy(), timeout=15000)
    page.refresh()
    assert page.count() == 0 and page.empty.isVisible()


def test_progress_text_before_and_during_copy() -> None:
    from contentshare.app.controller import ActiveCopy
    from contentshare.copy_engine import Progress

    waiting = ActiveCopy(1, "Prog", "Media", "APPROVED", None, False)
    assert progress_text(waiting) == "Waiting to start…"
    p = Progress("a/b.mxf", "verify", 10, 100, 500, 250, 500, 2_000_000, 65)
    text = progress_text(ActiveCopy(1, "Prog", "Media", "COPYING", p, False))
    assert "ETA 1 min 05 s" in text and "a/b.mxf" in text and "Copy ▸ [Verify]" in text


# --------------------------------------------------------------------------- history
def test_history_lists_filters_and_exports(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, qtbot: Any
) -> None:
    rig.ready_mail()
    rig.mail(message_id="h2", subject="Annual Day 2025", body="video")
    page = rig.window.history
    page.refresh()
    assert page.table.rowCount() == 2
    page.search.setText("annual")
    assert page.table.rowCount() == 1
    page.search.setText("")
    page.state.setCurrentIndex(page.state.findData("READY"))
    assert all("Ready" in (page.table.item(r, 5).text()) for r in range(page.table.rowCount()))
    page.state.setCurrentIndex(0)
    page.table.selectRow(0)
    assert "Audit trail" in page.detail.toPlainText()

    from PySide6.QtWidgets import QFileDialog

    out = tmp_path / "h.csv"
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(out), ""))
    )
    page.export()
    assert out.exists() and out.read_text(encoding="utf-8-sig").count("\n") >= 3
    assert "Exported" in rig.window.statusBar().currentMessage()


def test_history_shows_the_copy_time_of_each_request(rig: Rig, qtbot: Any) -> None:
    from contentshare.ui.history_page import COLUMNS

    rig.ready_mail()
    page = rig.window.history
    column = COLUMNS.index("Copy time")
    page.refresh()
    assert page.table.item(0, column).text() == ""  # nothing copied yet
    card = rig.cards()[0]
    card.confirm.setChecked(True)
    rig.click(card, "Approve and copy")
    # the request turns VERIFIED a moment before the job's finish time is written
    qtbot.waitUntil(
        lambda: (
            rig.controller.views()[0].state == "VERIFIED"
            and rig.controller.views()[0].copy_seconds is not None
        ),
        timeout=20000,
    )
    page.refresh()
    assert page.table.item(0, column).text().endswith(" s")
    page.table.selectRow(0)
    assert "Copy time:" in page.detail.toPlainText()


def test_history_export_failure_is_reported(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, boxes: Boxes, tmp_path: Path
) -> None:
    from PySide6.QtWidgets import QFileDialog

    bad = tmp_path / "no_such_folder" / "x.csv"
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(bad), ""))
    )
    rig.window.history.export()
    assert boxes.shown[-1][1] == "Export failed"


def test_history_matches_helper(rig: Rig) -> None:
    rig.ready_mail()
    view = rig.controller.views()[0]
    assert matches(view, "") and matches(view, "ANITA") and matches(view, "media team")
    assert not matches(view, "zzz-not-there")


# --------------------------------------------------------------------------- settings
def test_settings_round_trip_and_save(rig: Rig, tmp_path: Path) -> None:
    page: SettingsPage = rig.window.settings
    page.load()
    assert page.collect() == rig.controller.config
    page.ready_score.setValue(90)
    page.dry_run.setChecked(True)
    assert page.save()
    assert rig.controller.config.matching.auto_ready_score == 90
    saved = load_config(tmp_path / "config.yaml")
    assert saved.matching.auto_ready_score == 90 and saved.dry_run


def test_settings_reject_invalid_values_with_a_readable_message(rig: Rig, boxes: Boxes) -> None:
    page = rig.window.settings
    page.default_reply.setPlainText("Hello {nonsense}")
    assert not page.save()
    assert boxes.shown[-1][1] == "Check the settings"
    assert "default_reply_template" in boxes.last()
    page.load()
    page.roots.addItem("relative\\path")
    assert not page.save()
    assert "source_roots" in boxes.last()


# --------------------------------------------------------------------------- Telegram settings
class FakeTelegram:
    def __init__(self) -> None:
        self.token: str | None = None
        self.sent: list[tuple[str, str, str | None]] = []
        self.chats: list[Chat] = []
        self.error: Exception | None = None

    def has_token(self) -> bool:
        return bool(self.token)

    def save_token(self, token: str) -> None:
        if not valid_token(token):
            raise TelegramError("That does not look like a bot token.")
        self.token = token

    def forget_token(self) -> None:
        self.token = None

    def send(self, chat_id: str, text: str, *, token: str | None = None) -> None:
        if self.error:
            raise self.error
        self.sent.append((chat_id, text, token))

    def recent_chats(self, *, token: str | None = None) -> list[Chat]:
        if self.error:
            raise self.error
        return self.chats


TG_TOKEN = "123456789:AAH-abcdefghijklmnopqrstuvwxyz012345"


@pytest.fixture
def tg(rig: Rig) -> FakeTelegram:
    fake = FakeTelegram()
    rig.controller.telegram = fake  # type: ignore[assignment]
    rig.window.settings.load()
    return fake


def test_telegram_is_off_until_chosen_and_fields_follow_the_switch(
    rig: Rig, tg: FakeTelegram
) -> None:
    box = rig.window.settings.telegram_box
    assert not rig.controller.config.telegram.enabled
    assert not box.token.isEnabled() and not box.test.isEnabled()
    box.enabled.setChecked(True)
    assert box.token.isEnabled() and box.test.isEnabled() and box.find_group.isEnabled()


def test_saving_telegram_keeps_the_token_out_of_the_settings_file(
    rig: Rig, tg: FakeTelegram, tmp_path: Path
) -> None:
    page = rig.window.settings
    box = page.telegram_box
    box.enabled.setChecked(True)
    box.token.setText(TG_TOKEN)
    box.chat.setText("-1001234567890")
    assert page.save()
    assert tg.token == TG_TOKEN and box.token.text() == ""
    assert rig.controller.config.telegram.enabled
    assert rig.controller.config.telegram.chat_id == "-1001234567890"
    assert TG_TOKEN not in (tmp_path / "config.yaml").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("chat", "token", "fragment"),
    [
        ("", TG_TOKEN, "group number"),
        ("-100123", "", "bot token"),
        ("-100123", "nonsense", "bot token"),
    ],
)
def test_telegram_needs_a_group_and_a_token_to_be_switched_on(
    rig: Rig, boxes: Boxes, tg: FakeTelegram, chat: str, token: str, fragment: str
) -> None:
    page = rig.window.settings
    page.telegram_box.enabled.setChecked(True)
    page.telegram_box.chat.setText(chat)
    page.telegram_box.token.setText(token)
    assert not page.save()
    assert fragment in boxes.last() and not rig.controller.config.telegram.enabled


def test_a_test_message_uses_what_is_typed_and_says_so(
    rig: Rig, boxes: Boxes, tg: FakeTelegram, qtbot: Any
) -> None:
    box = rig.window.settings.telegram_box
    box.enabled.setChecked(True)
    box.token.setText(TG_TOKEN)
    box.chat.setText("-100555")
    box.test.click()
    qtbot.waitUntil(lambda: bool(boxes.shown), timeout=5000)
    assert tg.sent and tg.sent[0][0] == "-100555" and tg.sent[0][2] == TG_TOKEN
    assert "test message was sent" in boxes.last()


def test_a_failed_test_message_shows_the_reason(
    rig: Rig, boxes: Boxes, tg: FakeTelegram, qtbot: Any
) -> None:
    box = rig.window.settings.telegram_box
    box.enabled.setChecked(True)
    box.chat.setText("-100555")
    tg.token = TG_TOKEN
    tg.error = TelegramError("The bot is not allowed to post there.")
    box.test.click()
    qtbot.waitUntil(lambda: bool(boxes.shown), timeout=5000)
    assert "not allowed to post" in boxes.last()
    assert box.test.isEnabled()  # usable again


def test_find_my_group_fills_in_the_number(
    rig: Rig, boxes: Boxes, tg: FakeTelegram, qtbot: Any
) -> None:
    box = rig.window.settings.telegram_box
    box.enabled.setChecked(True)
    tg.token = TG_TOKEN
    tg.chats = [Chat("-100777", "Media")]
    box.find_group.click()
    qtbot.waitUntil(lambda: box.chat.text() == "-100777", timeout=5000)
    tg.chats = []
    box.find_group.click()
    qtbot.waitUntil(lambda: bool(boxes.shown), timeout=5000)
    assert "has not seen a group" in boxes.last()


def test_forgetting_the_token_asks_first(rig: Rig, boxes: Boxes, tg: FakeTelegram) -> None:
    box = rig.window.settings.telegram_box
    box.enabled.setChecked(True)
    tg.token = TG_TOKEN
    boxes.answer = QMessageBox.StandardButton.No
    box.forget.click()
    assert tg.token == TG_TOKEN
    boxes.answer = QMessageBox.StandardButton.Yes
    box.forget.click()
    assert tg.token is None


def test_settings_refused_while_copying(
    rig: Rig, boxes: Boxes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rig.controller, "busy", lambda: True)
    assert not rig.window.settings.save()
    assert "running copies" in boxes.last() or "Wait for" in boxes.last()


def test_department_editing(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    from contentshare.config.models import Department
    from contentshare.ui import settings_page as module

    page = rig.window.settings
    before = len(page.departments)
    new = Department(
        name="Archive",
        sender_emails=["z@arch.org"],
        destination_root=str(rig.world.media.parent / "arch"),
    )

    class FakeDept:
        result_department = new

        def __init__(self, *_a: Any, **_k: Any) -> None: ...
        def exec(self) -> int:
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(module, "DepartmentDialog", FakeDept)
    page._add_department()
    assert len(page.departments) == before + 1 and page.dept_table.rowCount() == before + 1
    page.dept_table.selectRow(before)
    page._edit_department()
    page.dept_table.selectRow(before)
    page._remove_department()
    assert len(page.departments) == before


def test_department_dialog_validates(qtbot: Any, boxes: Boxes) -> None:
    from contentshare.ui.settings_page import DepartmentDialog

    dialog = DepartmentDialog()
    qtbot.addWidget(dialog)
    dialog.name.setText("Media")
    dialog.emails.setPlainText("not-an-email")
    dialog.root.setText(r"D:\Incoming")
    dialog._accept()
    assert dialog.result_department is None and "Not a valid email" in boxes.last()
    dialog.emails.setPlainText("a@b.org")
    dialog.pattern.setText("{program}/{bogus}")
    dialog._accept()
    assert dialog.result_department is None
    dialog.pattern.setText("{program}/{date}")
    dialog._accept()
    assert (
        dialog.result_department
        and dialog.result_department.subfolder_pattern == "{program}/{date}"
    )


def test_settings_index_rescan(rig: Rig, qtbot: Any) -> None:
    page = rig.window.settings
    page.scan_button.click()
    qtbot.waitUntil(page.scan_button.isEnabled, timeout=15000)
    qtbot.waitUntil(lambda: "items indexed" in page.index_state.text())


def test_gmail_buttons_follow_connection_state(rig: Rig, qtbot: Any, boxes: Boxes) -> None:
    class FakeAuth:
        connected = False

        def is_connected(self) -> bool:
            return self.connected

        def connect(self, *_a: Any) -> None:
            self.connected = True

        def disconnect(self) -> Any:
            self.connected = False
            return type("O", (), {"revoked": True, "error": None})()

    auth = FakeAuth()
    rig.controller.auth = auth  # type: ignore[assignment]
    secret = Path(rig.world.media.parent / "client_secret.json")
    secret.write_text("{}")
    page = rig.window.settings
    page.load()
    page.secret.setText(str(secret))
    qtbot.waitUntil(page.connect_button.isEnabled)  # the state is read in the background
    assert not page.disconnect_button.isEnabled()
    page.connect_button.click()
    qtbot.waitUntil(lambda: auth.connected and page.disconnect_button.isEnabled(), timeout=10000)
    assert "Connected" in page.gmail_state.text()
    page.disconnect_button.click()
    qtbot.waitUntil(lambda: any(s[1] == "Disconnected" for s in boxes.shown), timeout=10000)
    assert page.gmail_state.text() == "Not connected"


# --------------------------------------------------------------------------- wizard / window
def test_wizard_builds_a_valid_config(qtbot: Any, boxes: Boxes, tmp_path: Path) -> None:
    from contentshare.config.models import AppConfig

    wizard = FirstRunWizard(AppConfig())
    qtbot.addWidget(wizard)
    wizard.roots.addItem(str(tmp_path / "archive"))
    wizard.dept_name.setText("Media Team")
    wizard.dept_emails.setPlainText("anita@dept.org")
    wizard.dept_root.setText(str(tmp_path / "out"))
    config = wizard.build_config()
    assert config.dry_run and config.departments[0].sender_emails == ["anita@dept.org"]
    wizard.accept()
    assert wizard.result_config == config


def test_wizard_rejects_bad_input_and_empty_storage(qtbot: Any, boxes: Boxes) -> None:
    from contentshare.config.models import AppConfig

    wizard = FirstRunWizard(AppConfig())
    qtbot.addWidget(wizard)
    wizard.show()
    wizard.next()  # welcome -> storage
    wizard.next()  # no folder yet: refused
    assert wizard.currentPage() is wizard.storage_page and "storage folder" in boxes.last()
    wizard.roots.addItem("Z:\\Programs")
    wizard.next()
    assert wizard.currentPage() is wizard.department_page
    wizard.dept_name.setText("Media")
    wizard.dept_emails.setPlainText("oops")
    wizard.dept_root.setText("D:\\x")
    wizard.next()
    assert wizard.currentPage() is wizard.department_page and "email" in boxes.last().lower()
    wizard.accept()
    assert wizard.result_config is None


def test_wizard_typed_path_helper(qtbot: Any) -> None:
    from contentshare.config.models import AppConfig

    wizard = FirstRunWizard(AppConfig())
    qtbot.addWidget(wizard)
    wizard.typed.setText(r"\\storage01\Programs")
    wizard._add_typed()
    assert wizard.roots.item(0).text() == r"\\storage01\Programs" and wizard.typed.text() == ""


def test_closing_while_copying_asks_first(
    rig: Rig, boxes: Boxes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rig.controller, "busy", lambda: True)
    boxes.answer = QMessageBox.StandardButton.No
    assert not rig.window.close()
    assert "Copies are still running" in boxes.last()
    monkeypatch.undo()
    assert rig.window.close()


def test_bridge_reports_errors_back_on_the_gui_thread(qtbot: Any) -> None:
    bridge = Bridge()
    seen: dict[str, Any] = {}

    def boom() -> None:
        raise ValueError("nope")

    bridge.work(boom, on_error=lambda e: seen.update(err=e, thread=threading.current_thread().name))
    qtbot.waitUntil(lambda: "err" in seen, timeout=5000)
    assert isinstance(seen["err"], ValueError) and seen["thread"] == "MainThread"
    bridge.work(lambda: 41 + 1, on_done=lambda r: seen.update(done=r))
    qtbot.waitUntil(lambda: "done" in seen, timeout=5000)
    assert seen["done"] == 42
    bridge.close()


def test_every_screen_can_be_grabbed_in_both_themes(rig: Rig) -> None:
    rig.ready_mail()
    app = QApplication.instance()
    for theme in (LIGHT, DARK):
        apply_theme(app, theme)  # type: ignore[arg-type]
        for row in range(4):
            rig.window.nav.setCurrentRow(row)
            assert not rig.window.grab().isNull()
    apply_theme(app)  # type: ignore[arg-type]


def test_checkboxes_have_accessible_names(rig: Rig) -> None:
    rig.ready_mail()
    card = rig.cards()[0]
    assert card.select.accessibleName() == f"Select request {card.view.id}"
    assert all(isinstance(c, QCheckBox) for c in (card.confirm, card.auto))


# --------------------------------------------------------------------------- dialogs and startup
def test_candidate_dialog_suggestions_search_and_choice(rig: Rig, qtbot: Any) -> None:
    from contentshare.ui.dialogs import CandidateDialog

    rig.mail(
        message_id="d1", from_="Boss <editor@dept.org>", subject="Sunrise Talk",
        body="12 Oct 2026 english",
        auth=auth_header("dept.org", dmarc="fail", spf="fail", dkim="fail"),
    )  # fmt: skip
    view = rig.controller.views()[0]
    dialog = CandidateDialog(rig.controller, view)
    qtbot.addWidget(dialog)
    assert dialog.results.count() == len(view.candidates) >= 1
    assert "Suggestions" in dialog.hint.text()
    dialog.query.setText("Annual Day 2025")
    dialog._search()
    assert any("AnnualDay" in dialog.results.item(i).text() for i in range(dialog.results.count()))
    dialog.query.setText("zzzz nothing")
    dialog._search()
    assert dialog.results.count() == 0 and "Nothing found" in dialog.hint.text()
    dialog.query.setText("   ")
    dialog._search()  # blank search does nothing
    dialog.accept()  # nothing selected: stays open
    assert dialog.chosen is None
    dialog.query.setText("Annual Day 2025")
    dialog._search()
    row = next(
        i for i in range(dialog.results.count()) if "AnnualDay" in dialog.results.item(i).text()
    )
    dialog.results.setCurrentRow(row)
    dialog.accept()
    assert dialog.chosen is not None and "AnnualDay" in dialog.chosen.path


def test_destination_and_reject_dialogs(
    rig: Rig, qtbot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtWidgets import QFileDialog

    from contentshare.ui.dialogs import DestinationDialog, RejectDialog

    dest = DestinationDialog(r"D:\Incoming", r"D:\Incoming\X")
    qtbot.addWidget(dest)
    assert dest.value() == r"D:\Incoming\X"
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: "D:/Incoming/Y")
    )
    dest._browse()
    assert dest.value() == r"D:\Incoming\Y"

    rig.ready_mail()
    reject = RejectDialog(rig.controller.views()[0])
    qtbot.addWidget(reject)
    assert not reject.text.isEnabled()
    reject.reason.setText(" not available ")
    reject.notify.setChecked(True)
    reject.text.setPlainText("Sorry")
    assert reject.text.isEnabled()
    assert reject.result_values() == ("not available", True, "Sorry")


def test_startup_helpers(isolated_home: Path, qtbot: Any) -> None:
    import logging

    from contentshare.ui import app as app_module

    controller = app_module.build_controller()
    try:
        assert app_module.needs_first_run(controller)
        assert (isolated_home / "contentshare.db").exists()
        with pytest.raises(Exception, match=r"not connected|Gmail"):
            controller._gateway_factory()
    finally:
        controller.shutdown()
    log_file = app_module.setup_logging(isolated_home / "logs")
    logging.getLogger("contentshare").info("hello")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert log_file.exists()
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "baseFilename", "").endswith("contentshare.log"):
            root.removeHandler(handler)
            handler.close()

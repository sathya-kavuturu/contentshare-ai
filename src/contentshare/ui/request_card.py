"""One request line as a card: what was asked, what was found, and what you can do about it."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from contentshare.app.controller import ApproveResult, CandidateView, RequestView
from contentshare.copy_engine import format_bytes
from contentshare.gmail.replies import ReplyResult
from contentshare.ui.context import UiContext
from contentshare.ui.dialogs import (
    CandidateDialog,
    DestinationDialog,
    RejectDialog,
    ReplyDialog,
)
from contentshare.ui.status import confidence_dots, style_for
from contentshare.ui.timefmt import local_stamp
from contentshare.ui.widgets import ElidedLabel
from contentshare.ui.widgets import Label as QLabel
from contentshare.ui.widgets import SafeMessageBox as QMessageBox

_GMAIL_ID = re.compile(r"^[0-9a-f]{6,32}$")
_INDENT = 64  # past the fold arrow and the tick box, so text lines up with the title
OPEN_STATES = {"NEW", "NEEDS_REVIEW", "READY", "APPROVED", "COPYING", "FAILED", "VERIFIED"}


@dataclass
class CardState:
    """What the user has ticked, kept across list refreshes."""

    selected: bool = False
    confirm: bool = False
    acknowledge: bool = False
    show_text: bool = False
    collapsed: bool = False  # folded to one short line (title, sender, department)


def _label(text: str = "", role: str | None = None, wrap: bool = True) -> QLabel:
    if not wrap and role != "section":  # one line, shortened in the middle if needed
        return ElidedLabel(text, role)
    label = QLabel(text)
    label.setWordWrap(wrap)
    if role:
        label.setProperty("role", role)
    return label


def _chip(text: str, tone: str) -> QLabel:
    chip = QLabel(text)
    chip.setProperty("tone", tone)
    chip.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    return chip


def _note_row(chip: QLabel) -> QHBoxLayout:
    """A chip that keeps its own width instead of stretching across the card."""
    row = QHBoxLayout()
    row.addWidget(chip)
    row.addStretch(1)
    return row


def _inset() -> QFrame:
    """A sunken panel inside the card, for the paths and matches."""
    frame = QFrame()
    frame.setObjectName("inset")
    return frame


def _divider() -> QFrame:
    line = QFrame()
    line.setObjectName("divider")
    line.setFrameShape(QFrame.Shape.NoFrame)
    return line


def _button(
    text: str, slot: Callable[[], None], *, primary: bool = False, danger: bool = False
) -> QPushButton:
    button = QPushButton(text)
    if primary:
        button.setProperty("primary", True)
    if danger:
        button.setProperty("danger", True)
    button.clicked.connect(lambda _checked=False: slot())
    return button


def gmail_url(message_id: str) -> QUrl | None:
    """Link to the email in Gmail. The id comes from Gmail, but is checked before it goes
    into a URL."""
    if not _GMAIL_ID.match(message_id):
        return None
    return QUrl(f"https://mail.google.com/mail/u/0/#all/{message_id}")


class RequestCard(QFrame):
    def __init__(
        self,
        view: RequestView,
        ctx: UiContext,
        state: CardState,
        *,
        on_selection_changed: Callable[[], None],
        on_fold_changed: Callable[[], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.view, self.ctx, self.state = view, ctx, state
        self._on_selection = on_selection_changed
        self._on_fold = on_fold_changed or (lambda: None)
        self.setObjectName("card")
        self.setAccessibleName(f"Request {view.id}")
        self._buttons: list[QPushButton] = []
        self._build()

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        v = self.view
        root = self._root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        self._build_head(root)

        # Everything else is folded away with the arrow.
        self.details = QWidget()
        body = QVBoxLayout(self.details)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(10)
        root.addWidget(self.details)

        who = f"{v.sender_name} · {v.sender_email}" if v.sender_name else v.sender_email
        meta = QHBoxLayout()
        meta.setSpacing(8)
        meta.addSpacing(_INDENT)
        meta.addWidget(
            _label(
                f"{who}  ·  {v.department or 'not in any department'}  ·  "
                f"{local_stamp(v.received_at)}  ·  #{v.id}",
                "small",
                wrap=False,
            ),
            1,
        )
        if v.verified_sender:
            meta.addWidget(_chip("✔ Sender verified", "neutral"))
        else:
            meta.addWidget(_chip("⚠ Sender NOT verified", "warn"))
        body.addLayout(meta)

        self._subject_row(body)
        if v.reason:
            body.addWidget(_label(v.reason, "muted"))
        for warning in v.warnings:
            if "could not be verified" in warning:
                continue  # the sender chip and the reason above already say it
            body.addLayout(_note_row(_chip(f"⚠ {warning}", "warn")))

        {
            "NEEDS_REVIEW": self._review_section,
            "READY": self._ready_section,
            "FAILED": self._failed_section,
            "APPROVED": self._copying_section,
            "COPYING": self._copying_section,
            "VERIFIED": self._verified_section,
        }.get(v.state, self._finished_section)(body)
        self.set_collapsed(self.state.collapsed)

    def _build_head(self, root: QVBoxLayout) -> None:
        v = self.view
        head = QHBoxLayout()
        head.setSpacing(10)
        self.fold = QPushButton("▾")
        self.fold.setProperty("fold", True)
        self.fold.setFlat(True)
        self.fold.clicked.connect(self.toggle_collapsed)
        head.addWidget(self.fold)
        self.select = QCheckBox()
        self.select.setAccessibleName(f"Select request {v.id}")
        self.select.setChecked(self.state.selected)
        self.select.toggled.connect(self._toggle_selected)
        head.addWidget(self.select)
        title = _label(v.requested or "(nothing recognised)", "heading", wrap=False)
        head.addWidget(title, 1)
        style = style_for(v.state)
        self.status = _chip(style.text, style.tone)
        head.addWidget(self.status)
        root.addLayout(head)

        # Folded: just who asked and for which department, under the title.
        self.summary = QWidget()
        short = QHBoxLayout(self.summary)
        short.setContentsMargins(0, 0, 0, 0)
        short.addSpacing(_INDENT)
        short.addWidget(
            _label(
                f"{v.sender_name or v.sender_email}  ·  {v.department or 'not in any department'}",
                "small",
                wrap=False,
            ),
            1,
        )
        root.addWidget(self.summary)

    # ------------------------------------------------------------------ folding
    def set_collapsed(self, collapsed: bool) -> None:
        """Fold the card to one short line, or open it again. Kept across list refreshes."""
        self.state.collapsed = collapsed
        self.details.setVisible(not collapsed)
        self.summary.setVisible(collapsed)
        self.fold.setText("▸" if collapsed else "▾")
        action = "Show" if collapsed else "Hide"
        self.fold.setToolTip(f"{action} the details of this request")
        self.fold.setAccessibleName(f"{action} details of request {self.view.id}")
        self._root.setContentsMargins(18, 10 if collapsed else 16, 18, 10 if collapsed else 16)
        self._root.setSpacing(4 if collapsed else 10)

    def toggle_collapsed(self) -> None:
        self.set_collapsed(not self.state.collapsed)
        self._on_fold()

    def _subject_row(self, root: QVBoxLayout) -> None:
        row = QHBoxLayout()
        row.addWidget(_label(f"Subject: {self.view.subject}", "muted", wrap=False), 1)
        self.text_toggle = QPushButton("▸ Show text")
        self.text_toggle.setFlat(True)
        self.text_toggle.setAccessibleName("Show the start of the email")
        self.text_toggle.clicked.connect(self._toggle_text)
        row.addWidget(self.text_toggle)
        root.addLayout(row)
        self.snippet = _label(self.view.snippet or "(no text)", "quote")
        self.snippet.setVisible(self.state.show_text)
        root.addWidget(self.snippet)

    def _add_actions(self, root: QVBoxLayout, *buttons: QPushButton) -> None:
        root.addWidget(_divider())
        row = QHBoxLayout()
        row.setSpacing(8)
        for button in buttons:
            self._buttons.append(button)
            row.addWidget(button)
        row.addStretch(1)
        gmail = _button("Open email in Gmail", self._open_gmail)
        gmail.setFlat(True)
        row.addWidget(gmail)
        root.addLayout(row)

    # ------------------------------------------------------------------ state sections
    def _review_section(self, root: QVBoxLayout) -> None:
        v = self.view
        if v.candidates:
            root.addWidget(_label("TOP MATCHES", "section", wrap=False))
        shown = v.candidates[:5]
        if shown:
            box = _inset()
            inner = QVBoxLayout(box)
            inner.setContentsMargins(14, 6, 10, 6)
            inner.setSpacing(0)
            for position, candidate in enumerate(shown):
                if position:
                    inner.addWidget(_divider())
                inner.addLayout(self._candidate_row(candidate))
            root.addWidget(box)
        self._add_actions(
            root,
            _button("Search manually…", self._search_manually, primary=not v.candidates),
            _button("Skip", self._skip),
            _button("Reject…", self._reject, danger=True),
        )

    def _candidate_row(self, candidate: CandidateView) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 8, 0, 8)
        row.setSpacing(12)
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(_label(candidate.path, "path", wrap=False))
        text.addWidget(
            _label(
                f"{format_bytes(candidate.size)} · {candidate.file_count} file(s) · "
                f"{confidence_dots(candidate.score)}"
                + (f" · {'; '.join(candidate.notes)}" if candidate.notes else ""),
                "small",
                wrap=False,
            )
        )
        row.addLayout(text, 1)
        use = _button("Use this", partial(self._choose, candidate.item_id))
        use.setAccessibleName(f"Use {candidate.path}")
        self._buttons.append(use)
        row.addWidget(use, 0, Qt.AlignmentFlag.AlignVCenter)
        return row

    def _ready_section(self, root: QVBoxLayout) -> None:
        v = self.view
        box = _inset()
        grid = QGridLayout(box)
        grid.setContentsMargins(14, 12, 14, 12)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)
        grid.addWidget(_label("FOUND", "section", wrap=False), 0, 0)
        grid.addWidget(_label(v.source_path or "", "path", wrap=False), 0, 1)
        grid.addWidget(_label("COPY TO", "section", wrap=False), 1, 0)
        grid.addWidget(_label(v.destination_path or "", "path", wrap=False), 1, 1)
        grid.addWidget(
            _label(
                f"{v.size_text} · {v.file_count} file(s) · "
                f"confidence {confidence_dots(v.confidence)}",
                "small",
                wrap=False,
            ),
            2,
            1,
        )
        root.addWidget(box)

        change = QHBoxLayout()
        change.setSpacing(8)
        for text, slot in (
            ("Change content…", self._search_manually),
            ("Edit destination…", self._edit_destination),
        ):
            button = _button(text, slot)
            button.setFlat(True)
            change.addWidget(button)
        change.addStretch(1)
        root.addLayout(change)

        self.confirm = QCheckBox("Confirm copy")
        self.confirm.setChecked(self.state.confirm)
        self.confirm.toggled.connect(self._toggle_confirm)
        root.addWidget(self.confirm)
        self.auto = QCheckBox("Send the reply automatically when the copy is verified")
        self.auto.setChecked(v.auto_reply)
        self.auto.toggled.connect(self._toggle_auto)
        root.addWidget(self.auto)
        self.ack: QCheckBox | None = None
        if not v.verified_sender:
            self.ack = QCheckBox("I checked this sender in Gmail and it is genuine")
            self.ack.setChecked(self.state.acknowledge)
            self.ack.toggled.connect(self._toggle_ack)
            root.addWidget(self.ack)
        verb = "Check (dry run)" if self.ctx.controller.dry_run else "Approve and copy"
        self._add_actions(
            root,
            _button(verb, self._approve, primary=True),
            _button("Skip", self._skip),
            _button("Reject…", self._reject, danger=True),
        )

    def _failed_section(self, root: QVBoxLayout) -> None:
        v = self.view
        root.addLayout(_note_row(_chip(f"✖ {v.failure_reason or 'The copy failed'}", "error")))
        root.addWidget(_label("No “shared” reply can be sent until a copy is verified.", "muted"))
        self._add_actions(
            root,
            _button("Retry copy", self._retry, primary=True),
            _button("Change content…", self._search_manually),
            _button("Skip", self._skip),
        )

    def _copying_section(self, root: QVBoxLayout) -> None:
        root.addWidget(_label("Copying. Progress is on the Active copies screen.", "muted"))
        self._add_actions(root)

    def _verified_section(self, root: QVBoxLayout) -> None:
        v = self.view
        root.addWidget(
            _label(
                f"✔✔ {v.file_count} file(s), {v.size_text} copied to {v.destination_path} "
                "and checked against the original.",
            )
        )
        self._add_actions(root, _button("Preview and send reply…", self._reply, primary=True))

    def _finished_section(self, root: QVBoxLayout) -> None:
        v = self.view
        if v.destination_path and v.state == "REPLIED":
            root.addWidget(_label(f"Delivered to {v.destination_path}", "muted"))
        self._add_actions(root)

    # ------------------------------------------------------------------ small slots
    def _toggle_selected(self, on: bool) -> None:
        self.state.selected = on
        self._on_selection()

    def _toggle_confirm(self, on: bool) -> None:
        self.state.confirm = on

    def _toggle_ack(self, on: bool) -> None:
        self.state.acknowledge = on

    def _toggle_text(self) -> None:
        self.state.show_text = not self.state.show_text
        self.snippet.setVisible(self.state.show_text)
        self.text_toggle.setText("▾ Hide text" if self.state.show_text else "▸ Show text")

    def _toggle_auto(self, on: bool) -> None:
        try:
            self.ctx.controller.set_auto_reply(self.view.id, on)
        except Exception as exc:
            self.ctx.fail(self, "Cannot change this", exc)

    def _open_gmail(self) -> None:
        url = gmail_url(self.view.gmail_message_id)
        if url is None:
            QMessageBox.information(self, "Open email", "Gmail's link for this email is missing.")
        else:
            QDesktopServices.openUrl(url)

    def _alive(self) -> bool:
        """The list is rebuilt when anything changes, so a card can be gone by the time a
        background job reports back."""
        return isValid(self)

    def _owner(self) -> QWidget | None:
        return self if self._alive() else self.ctx.owner

    def _busy(self, on: bool) -> None:
        if not self._alive():
            return
        for button in self._buttons:
            button.setEnabled(not on)

    def _on_error(self, title: str) -> Callable[[Exception], None]:
        def handler(exc: Exception) -> None:
            self._busy(False)
            self.ctx.fail(self._owner(), title, exc)

        return handler

    # ------------------------------------------------------------------ actions
    def _search_manually(self) -> None:
        dialog = CandidateDialog(self.ctx.controller, self.view, self)
        if not dialog.exec():
            return
        if dialog.chosen_path is not None:
            self._choose_path(dialog.chosen_path)
        elif dialog.chosen is not None:
            self._choose(dialog.chosen.item_id)

    def _choose_path(self, path: str) -> None:
        self._busy(True)
        self.ctx.bridge.work(
            lambda: self.ctx.controller.choose_path(self.view.id, path),
            on_done=lambda _r: self.ctx.say(f"Request #{self.view.id}: content chosen"),
            on_error=self._on_error("Cannot use that"),
        )

    def _choose(self, item_id: int | None) -> None:
        if item_id is None:
            return
        self._busy(True)
        self.ctx.bridge.work(
            lambda: self.ctx.controller.choose(self.view.id, item_id),
            on_done=lambda _r: self.ctx.say(f"Request #{self.view.id}: content chosen"),
            on_error=self._on_error("Cannot use that"),
        )

    def _edit_destination(self) -> None:
        dept = next(
            (d for d in self.ctx.controller.config.departments if d.name == self.view.department),
            None,
        )
        if dept is None:
            QMessageBox.warning(
                self, "Destination", "This sender's department is not in the settings."
            )
            return
        dialog = DestinationDialog(dept.destination_root, self.view.destination_path or "", self)
        if dialog.exec():
            try:
                self.ctx.controller.set_destination(self.view.id, dialog.value())
            except Exception as exc:
                self.ctx.fail(self, "Cannot use that folder", exc)

    def _approve(self) -> None:
        self._busy(True)
        confirmed = self.state.confirm
        acknowledged = self.state.acknowledge
        self.ctx.bridge.work(
            lambda: self.ctx.controller.approve(
                self.view.id, confirmed=confirmed, acknowledge_unverified=acknowledged
            ),
            on_done=self._approved,
            on_error=self._on_error("Cannot approve"),
        )

    def _approved(self, result: ApproveResult) -> None:
        self._busy(False)
        if not result.dry_run:
            self.ctx.say(f"Request #{self.view.id}: copy started. {result.note}".strip())
        else:
            lines = [f"{result.files} file(s), {format_bytes(result.total_bytes)} would be copied."]
            if result.note:
                lines.append(result.note)
            if result.problems:
                lines += [
                    "",
                    "Problems that would stop the copy:",
                    *[f"• {p}" for p in result.problems],
                ]
            else:
                lines.append("No problems found. Nothing was copied.")
            QMessageBox.information(self._owner(), "Dry run", "\n".join(lines))

    def _retry(self) -> None:
        try:
            self.ctx.controller.retry(self.view.id)
            self.ctx.say(f"Request #{self.view.id}: copy restarted")
        except Exception as exc:
            self.ctx.fail(self, "Cannot retry", exc)

    def _skip(self) -> None:
        answer = QMessageBox.question(
            self, "Skip request", f"Skip request #{self.view.id}? It will be kept in the history."
        )
        if answer == QMessageBox.StandardButton.Yes:
            try:
                self.ctx.controller.skip(self.view.id)
            except Exception as exc:
                self.ctx.fail(self, "Cannot skip", exc)

    def _reject(self) -> None:
        dialog = RejectDialog(self.view, self)
        if not dialog.exec():
            return
        reason, notify, text = dialog.result_values()
        try:
            self.ctx.controller.reject(self.view.id, reason)
        except Exception as exc:
            self.ctx.fail(self, "Cannot reject", exc)
            return
        if notify:
            self._send_rejection(text)

    def _send_rejection(self, text: str) -> None:
        def job() -> ReplyResult:
            c = self.ctx.controller
            return c.send_reply(c.rejection_draft(self.view.id, text or None))

        self.ctx.bridge.work(
            job,
            on_done=lambda r: self.ctx.say(f"Rejection reply: {r.status.lower()}"),
            on_error=self._on_error("The rejection reply was not sent"),
        )

    def _reply(self) -> None:
        try:
            draft = self.ctx.controller.reply_draft(self.view.id)
        except Exception as exc:
            self.ctx.fail(self, "Cannot prepare the reply", exc)
            return
        summary = (
            f"✔ All {self.view.file_count} file(s) were verified. The reply goes to "
            f"{draft.to_addr} in the same Gmail conversation."
        )
        dialog = ReplyDialog(
            draft, summary=summary, dry_run=self.ctx.controller.dry_run, parent=self
        )
        if not dialog.exec():
            return
        edited = dialog.draft()
        self._busy(True)
        self.ctx.bridge.work(
            lambda: self.ctx.controller.send_reply(edited),
            on_done=self._replied,
            on_error=self._on_error("Reply not sent"),
        )

    def _replied(self, result: ReplyResult) -> None:
        self._busy(False)
        if result.status == "SENT":
            self.ctx.say(f"Request #{self.view.id}: reply sent")
        elif result.status == "UNCERTAIN":
            QMessageBox.warning(
                self._owner(), "Reply status unknown",
                "Gmail could not be reached to confirm whether the reply was sent. "
                "ContentShare will check again later and will not send it twice.",
            )  # fmt: skip
        else:
            QMessageBox.warning(
                self._owner(), "Reply not sent", result.error or "The reply was not sent."
            )

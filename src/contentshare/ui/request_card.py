"""One email's request as a card: what was asked, what was found, and what you can do about it.

An email that lists several folders is ONE card with a section for each folder. Each section shows
the folder that was asked for and the path chosen for it on the storage server; when several
paths have that name, all of them are listed and the person chooses which one to use.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from contentshare.app.controller import (
    CandidateView,
    EmailGroup,
    GroupApproveResult,
    RequestView,
)
from contentshare.copy_engine import format_bytes
from contentshare.gmail.replies import ReplyResult
from contentshare.ui.context import UiContext
from contentshare.ui.dialogs import (
    AddFolderDialog,
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
_CHOICES_IN_VIEW = 6  # more paths than this are listed in a box that scrolls
_CHOICE_ROW_HEIGHT = 58
OPEN_STATES = {"NEW", "NEEDS_REVIEW", "READY", "APPROVED", "COPYING", "FAILED", "VERIFIED"}
_CAN_SKIP = {"NEEDS_REVIEW", "READY", "FAILED"}
_CAN_REJECT = {"NEEDS_REVIEW", "READY"}


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
    """A chip that keeps its own width instead of stretching across the card. A long text
    wraps inside the card: it must never make the card wider than the window."""
    chip.setWordWrap(True)
    chip.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    row = QHBoxLayout()
    row.addWidget(chip)
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


# What an email with several folders says about itself: the first line that applies is shown.
_GROUP_STATUS = (
    (("NEEDS_REVIEW",), "⚠ {n} of {total} need review", "warn"),
    (("FAILED",), "✖ {n} of {total} failed", "error"),
    (("APPROVED", "COPYING"), "▶ Copying {n} of {total}", "info"),
    (("READY",), "✔ {n} of {total} ready", "ready"),
    (("VERIFIED",), "✔✔ {n} of {total} verified", "ready"),
    (("REPLIED",), "✉ {n} of {total} replied", "neutral"),
)


def group_status(group: EmailGroup) -> tuple[str, str]:
    """The text and the tone of the one chip that stands for the whole email."""
    items = group.items
    if not group.many:
        style = style_for(items[0].state)
        return style.text, style.tone
    total = len(items)
    for states, text, tone in _GROUP_STATUS:
        if n := sum(1 for v in items if v.state in states):
            return (
                "✉ Replied" if n == total and tone == "neutral" else text.format(n=n, total=total)
            ), tone
    return "↷ Closed", "neutral"


class RequestCard(QFrame):
    def __init__(
        self,
        group: EmailGroup | RequestView,
        ctx: UiContext,
        state: CardState,
        *,
        on_selection_changed: Callable[[], None],
        on_fold_changed: Callable[[], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if isinstance(group, RequestView):  # a lone line is an email with one line
            group = EmailGroup(group.email_id, (group,))
        self.group = group
        self.view = (
            group.lead
        )  # the email's details (sender, subject, ...) are the same on every line
        self.ctx, self.state = ctx, state
        self._on_selection = on_selection_changed
        self._on_fold = on_fold_changed or (lambda: None)
        self.setObjectName("card")
        self.setAccessibleName(f"Request {self.view.id}")
        self._buttons: list[QPushButton] = []
        self._build()

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        v, group = self.view, self.group
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
        number = f"#{v.id}" if not group.many else f"#{v.id} to #{group.items[-1].id}"
        meta = QHBoxLayout()
        meta.setSpacing(8)
        meta.addSpacing(_INDENT)
        meta.addWidget(
            _label(
                f"{who}  ·  {v.department or 'not in any department'}  ·  "
                f"{local_stamp(v.received_at)}  ·  {number}",
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
        seen: set[str] = set()
        for item in group.items:
            for warning in item.warnings:
                if "could not be verified" in warning or warning in seen:
                    continue  # the sender chip and the reason already say it; once is enough
                seen.add(warning)
                body.addLayout(_note_row(_chip(f"⚠ {warning}", "warn")))

        for number_, item in enumerate(group.items, start=1):
            if group.many:
                body.addWidget(_divider())
            self._build_item(body, item, number_ if group.many else None)
        self._build_footer(body)
        self.set_collapsed(self.state.collapsed)

    def _build_head(self, root: QVBoxLayout) -> None:
        v, group = self.view, self.group
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
        if group.many:
            title_text = f"{v.subject.strip() or 'Request'}  ·  {len(group.items)} folders"
        else:
            title_text = v.requested or "(nothing recognised)"
        title = _label(title_text, "heading", wrap=False)
        head.addWidget(title, 1)
        text, tone = group_status(group)
        self.status = _chip(text, tone)
        head.addWidget(self.status)
        self.refresh_button = _button("↻", self._refresh)
        self.refresh_button.setToolTip(
            "Look the folders of this email up again in the index. The storage folders are "
            "read again first, so a folder made since then is found."
        )
        self.refresh_button.setAccessibleName(f"Refresh request {v.id}")
        self.ask_button = _button("Ask model", self._ask_model)
        self.ask_button.setToolTip(
            "Read this email again with the local model and add the folders it finds "
            "(needs the smart reading model, Settings → Smart reading)."
        )
        self.ask_button.setAccessibleName(f"Ask the local model about request {v.id}")
        for button in (self.refresh_button, self.ask_button):
            button.setFlat(True)
            self._buttons.append(button)
            head.addWidget(button)
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

    def _item_buttons(self, root: QVBoxLayout, *buttons: QPushButton, flat: bool = False) -> None:
        """Buttons of one folder, in a row under it."""
        row = QHBoxLayout()
        row.setSpacing(8)
        for button in buttons:
            button.setFlat(flat)
            self._buttons.append(button)
            row.addWidget(button)
        row.addStretch(1)
        root.addLayout(row)

    # ------------------------------------------------------------------ one folder of the email
    def _build_item(self, root: QVBoxLayout, v: RequestView, number: int | None) -> None:
        """The section of one requested folder (without a heading when it is the only one)."""
        if number is not None:
            head = QHBoxLayout()
            head.setSpacing(10)
            head.addWidget(_label(f"{number}.  {v.requested}", "heading", wrap=False), 1)
            style = style_for(v.state)
            head.addWidget(_chip(style.text, style.tone))
            root.addLayout(head)
        if v.reason:
            root.addWidget(_label(v.reason, "muted"))
        {
            "NEEDS_REVIEW": self._review_section,
            "READY": self._ready_section,
            "FAILED": self._failed_section,
            "APPROVED": self._copying_section,
            "COPYING": self._copying_section,
            "VERIFIED": self._verified_section,
        }.get(v.state, self._finished_section)(root, v)
        if number is not None and v.state in _CAN_SKIP:
            skip = _button("Skip this folder", partial(self._skip_item, v))
            self._item_buttons(root, skip, flat=True)

    def _review_section(self, root: QVBoxLayout, v: RequestView) -> None:
        choices = v.candidates
        if choices:
            title = f"{len(choices)} PATHS TO CHOOSE FROM" if len(choices) > 1 else "TOP MATCH"
            root.addWidget(_label(title, "section", wrap=False))
            root.addWidget(self._choices(v, choices))
        self._item_buttons(
            root,
            _button("Search manually…", partial(self._search_manually, v), primary=not choices),
        )

    def _choices(self, v: RequestView, choices: tuple[CandidateView, ...]) -> QWidget:
        """Every path found, each with a button to use it. A long list scrolls inside its box."""
        box = _inset()
        inner = QVBoxLayout(box)
        inner.setContentsMargins(14, 6, 10, 6)
        inner.setSpacing(0)
        for position, candidate in enumerate(choices):
            if position:
                inner.addWidget(_divider())
            inner.addLayout(self._candidate_row(v, candidate))
        if len(choices) <= _CHOICES_IN_VIEW:
            return box
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(box)
        scroll.setFixedHeight(_CHOICES_IN_VIEW * _CHOICE_ROW_HEIGHT)
        scroll.setAccessibleName("Paths to choose from")
        return scroll

    def _candidate_row(self, v: RequestView, candidate: CandidateView) -> QHBoxLayout:
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
        use = _button("Use this", partial(self._choose, v, candidate.item_id))
        use.setAccessibleName(f"Use {candidate.path}")
        self._buttons.append(use)
        row.addWidget(use, 0, Qt.AlignmentFlag.AlignVCenter)
        return row

    def _ready_section(self, root: QVBoxLayout, v: RequestView) -> None:
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
        self._item_buttons(
            root,
            _button("Change content…", partial(self._search_manually, v)),
            _button("Edit destination…", partial(self._edit_destination, v)),
            flat=True,
        )

    def _failed_section(self, root: QVBoxLayout, v: RequestView) -> None:
        root.addLayout(_note_row(_chip(f"✖ {v.failure_reason or 'The copy failed'}", "error")))
        root.addWidget(_label("No “shared” reply can be sent until a copy is verified.", "muted"))
        self._item_buttons(
            root,
            _button("Retry copy", partial(self._retry, v), primary=True),
            _button("Change content…", partial(self._search_manually, v)),
        )

    def _copying_section(self, root: QVBoxLayout, v: RequestView) -> None:
        root.addWidget(_label("Copying. Progress is on the Active copies screen.", "muted"))

    def _verified_section(self, root: QVBoxLayout, v: RequestView) -> None:
        root.addWidget(
            _label(
                f"✔✔ {v.file_count} file(s), {v.size_text} copied to {v.destination_path} "
                "and checked against the original.",
            )
        )

    def _finished_section(self, root: QVBoxLayout, v: RequestView) -> None:
        if v.destination_path and v.state == "REPLIED":
            root.addWidget(_label(f"Delivered to {v.destination_path}", "muted"))

    # ------------------------------------------------------------------ what applies to the email
    def _build_footer(self, root: QVBoxLayout) -> None:
        group, v = self.group, self.view
        ready = group.in_states({"READY"})
        verified = group.in_states({"VERIFIED"})
        buttons: list[QPushButton] = []
        self.confirm: QCheckBox | None = None
        self.auto: QCheckBox | None = None
        self.ack: QCheckBox | None = None
        if ready:
            self.confirm = QCheckBox("Confirm copy")
            self.confirm.setChecked(self.state.confirm)
            self.confirm.toggled.connect(self._toggle_confirm)
            root.addWidget(self.confirm)
            self.auto = QCheckBox("Send the reply automatically when the copy is verified")
            self.auto.setChecked(all(r.auto_reply for r in ready))
            self.auto.toggled.connect(self._toggle_auto)
            root.addWidget(self.auto)
            if not v.verified_sender:
                self.ack = QCheckBox("I checked this sender in Gmail and it is genuine")
                self.ack.setChecked(self.state.acknowledge)
                self.ack.toggled.connect(self._toggle_ack)
                root.addWidget(self.ack)
            verb = "Check (dry run)" if self.ctx.controller.dry_run else "Approve and copy"
            if group.many:
                verb += f" ({len(ready)} ready)"
            buttons.append(_button(verb, self._approve, primary=True))
        if verified:
            buttons.append(_button("Preview and send reply…", self._reply, primary=not ready))
        if group.lead.department is None and self.ctx.controller.config.departments:
            buttons.append(
                _button("Choose department…", self._choose_department, primary=not ready)
            )  # a sender in no department: say which one this is for
        buttons.append(_button("Add a folder…", self._add_folder))  # on every card, always
        if group.in_states(_CAN_SKIP):
            buttons.append(_button("Skip all" if group.many else "Skip", self._skip))
        if group.in_states(_CAN_REJECT):
            buttons.append(_button("Reject…", self._reject, danger=True))
        self._add_actions(root, *buttons)

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

    def _refresh(self) -> None:
        self._busy(True)
        controller, email_id = self.ctx.controller, self.view.email_id
        self.ctx.bridge.work(
            lambda: controller.refresh_email(email_id),
            on_done=self._looked_again,
            on_error=self._on_error("Cannot refresh"),
        )

    def _ask_model(self) -> None:
        self._busy(True)
        self.ctx.say(
            "Asking the local model… the first time it has to start, which can take a minute."
        )
        controller, email_id = self.ctx.controller, self.view.email_id
        self.ctx.bridge.work(
            lambda: controller.ask_model_about_email(email_id),
            on_done=self._looked_again,
            on_error=self._on_error("Cannot ask the model"),
        )

    def _looked_again(self, message: str) -> None:
        self._busy(False)
        self.ctx.say(message)

    def _toggle_auto(self, on: bool) -> None:
        try:
            self.ctx.controller.set_auto_reply_group(self.view.email_id, on)
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

    # ------------------------------------------------------------------ a sender in no department
    def _choose_department(self) -> None:
        names = [d.name for d in self.ctx.controller.config.departments]
        chosen, ok = QInputDialog.getItem(
            self,
            "Choose department",
            "This request is for the department:\n(its folders are searched, and the content "
            "is copied to its destination)",
            names,
            0,
            False,
        )
        if not ok or not chosen:
            return
        try:
            self.ctx.controller.assign_department(self.group.email_id, chosen)
        except Exception as exc:
            self.ctx.fail(self, "Cannot choose this department", exc)

    # ------------------------------------------------------------------ add a folder to the email
    def _unsearched_lines(self) -> list[str]:
        """The lines named in the card's "not searched" note."""
        lines: list[str] = []
        for item in self.group.items:
            for warning in item.warnings:
                if "were not searched" in warning:
                    found = re.findall("“([^”]+)”", warning)
                    lines += [t for t in found if t not in lines]
        return lines

    def _start_folder(self) -> str:
        """Where the Browse windows open: the department's first storage folder."""
        config = self.ctx.controller.config
        dept = next((d for d in config.departments if d.name == self.view.department), None)
        roots = config.roots_for(dept)
        return roots[0] if roots else ""

    def _add_folder(self) -> None:
        ask = AddFolderDialog(self._unsearched_lines(), self, start_folder=self._start_folder())
        if not ask.exec():
            return
        controller = self.ctx.controller
        name = Path(ask.picked).name or ask.picked if ask.picked else ask.value()
        try:
            request_id = controller.add_folder(self.group.email_id, name)
            view = controller.view(request_id)
        except Exception as exc:
            self.ctx.fail(self, "Cannot add this folder", exc)
            return
        if ask.picked:  # browsed: no search is needed
            self._choose_added(view, ask.picked)
            return
        dialog = CandidateDialog(controller, view, self, start_text=ask.value())
        if dialog.exec():
            if dialog.chosen_path is not None:
                self._choose_path(view, dialog.chosen_path)
                return
            if dialog.chosen is not None and dialog.chosen.item_id is not None:
                self._choose(view, dialog.chosen.item_id)
                return
        try:
            controller.discard_added_folder(request_id)  # nothing chosen: no empty line is left
        except Exception as exc:
            self.ctx.fail(self, "Cannot add this folder", exc)

    def _choose_added(self, view: RequestView, path: str) -> None:
        """Use a browsed folder or file for the line just added; if it cannot be used, the empty
        line is taken away again."""
        controller = self.ctx.controller
        self._busy(True)

        def failed(exc: Exception) -> None:
            with contextlib.suppress(Exception):
                controller.discard_added_folder(view.id)
            self._on_error("Cannot use that")(exc)

        self.ctx.bridge.work(
            lambda: controller.choose_path(view.id, path),
            on_done=lambda _r: self.ctx.say(f"Request #{view.id}: folder added"),
            on_error=failed,
        )

    # ------------------------------------------------------------------ actions on one folder
    def _search_manually(self, view: RequestView) -> None:
        dialog = CandidateDialog(self.ctx.controller, view, self)
        if not dialog.exec():
            return
        if dialog.chosen_path is not None:
            self._choose_path(view, dialog.chosen_path)
        elif dialog.chosen is not None:
            self._choose(view, dialog.chosen.item_id)

    def _choose_path(self, view: RequestView, path: str) -> None:
        self._busy(True)
        self.ctx.bridge.work(
            lambda: self.ctx.controller.choose_path(view.id, path),
            on_done=lambda _r: self.ctx.say(f"Request #{view.id}: content chosen"),
            on_error=self._on_error("Cannot use that"),
        )

    def _choose(self, view: RequestView, item_id: int | None) -> None:
        if item_id is None:
            return
        self._busy(True)
        self.ctx.bridge.work(
            lambda: self.ctx.controller.choose(view.id, item_id),
            on_done=lambda _r: self.ctx.say(f"Request #{view.id}: content chosen"),
            on_error=self._on_error("Cannot use that"),
        )

    def _edit_destination(self, view: RequestView) -> None:
        dept = next(
            (d for d in self.ctx.controller.config.departments if d.name == view.department),
            None,
        )
        if dept is None:
            QMessageBox.warning(
                self, "Destination", "This sender's department is not in the settings."
            )
            return
        dialog = DestinationDialog(dept.destination_root, view.destination_path or "", self)
        if dialog.exec():
            try:
                self.ctx.controller.set_destination(view.id, dialog.value())
            except Exception as exc:
                self.ctx.fail(self, "Cannot use that folder", exc)

    def _retry(self, view: RequestView) -> None:
        try:
            self.ctx.controller.retry(view.id)
            self.ctx.say(f"Request #{view.id}: copy restarted")
        except Exception as exc:
            self.ctx.fail(self, "Cannot retry", exc)

    def _skip_item(self, view: RequestView) -> None:
        answer = QMessageBox.question(
            self, "Skip folder", f"Skip “{view.requested}”? It will be kept in the history."
        )
        if answer == QMessageBox.StandardButton.Yes:
            try:
                self.ctx.controller.skip(view.id)
            except Exception as exc:
                self.ctx.fail(self, "Cannot skip", exc)

    # ------------------------------------------------------------------ actions on the email
    def _approve(self) -> None:
        self._busy(True)
        confirmed = self.state.confirm
        acknowledged = self.state.acknowledge
        email_id = self.view.email_id
        self.ctx.bridge.work(
            lambda: self.ctx.controller.approve_group(
                email_id, confirmed=confirmed, acknowledge_unverified=acknowledged
            ),
            on_done=self._approved,
            on_error=self._on_error("Cannot approve"),
        )

    def _approved(self, result: GroupApproveResult) -> None:
        self._busy(False)
        notes = " ".join(result.notes)
        if not result.dry_run:
            what = "copy started" if result.started == 1 else f"{result.started} copies started"
            self.ctx.say(f"Request #{self.view.id}: {what}. {notes}".strip())
            if result.problems:
                QMessageBox.warning(
                    self._owner(), "Some folders were not started", "\n".join(result.problems)
                )
            return
        lines = [f"{result.files} file(s), {format_bytes(result.total_bytes)} would be copied."]
        lines += list(result.notes)
        if result.problems:
            lines += [
                "",
                "Problems that would stop the copy:",
                *[f"• {p}" for p in result.problems],
            ]
        else:
            lines.append("No problems found. Nothing was copied.")
        QMessageBox.information(self._owner(), "Dry run", "\n".join(lines))

    def _skip(self) -> None:
        if self.group.many:
            open_lines = len(self.group.in_states(_CAN_SKIP))
            question = (
                f"Skip the {open_lines} folder(s) of this email that are still open? "
                "They will be kept in the history."
            )
        else:
            question = f"Skip request #{self.view.id}? It will be kept in the history."
        if QMessageBox.question(self, "Skip request", question) != QMessageBox.StandardButton.Yes:
            return
        try:
            self.ctx.controller.skip_group(self.view.email_id)
        except Exception as exc:
            self.ctx.fail(self, "Cannot skip", exc)

    def _reject(self) -> None:
        summary = (
            f"{self.view.subject.strip() or 'Request'}: {len(self.group.items)} folders"
            if self.group.many
            else None
        )
        dialog = RejectDialog(self.view, self, summary=summary)
        if not dialog.exec():
            return
        reason, notify, text = dialog.result_values()
        try:
            rejected = self.ctx.controller.reject_group(self.view.email_id, reason)
        except Exception as exc:
            self.ctx.fail(self, "Cannot reject", exc)
            return
        if notify:
            self._send_rejection(rejected[0], text)

    def _send_rejection(self, request_id: int, text: str) -> None:
        def job() -> ReplyResult:
            c = self.ctx.controller
            return c.send_reply(c.rejection_draft(request_id, text or None))

        self.ctx.bridge.work(
            job,
            on_done=lambda r: self.ctx.say(f"Rejection reply: {r.status.lower()}"),
            on_error=self._on_error("The rejection reply was not sent"),
        )

    def _reply(self) -> None:
        try:
            draft = self.ctx.controller.reply_draft_for_email(self.view.email_id)
        except Exception as exc:
            self.ctx.fail(self, "Cannot prepare the reply", exc)
            return
        covered = {draft.request_id, *draft.also}
        lines = [v for v in self.group.items if v.id in covered]
        files = sum(v.file_count or 0 for v in lines)
        folders = f" in {len(lines)} folders" if len(lines) > 1 else ""
        summary = (
            f"✔ All {files} file(s){folders} were verified. The reply goes to "
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

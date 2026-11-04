"""The Requests screen: check mail, review what was found, approve."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from contentshare.app.controller import Event
from contentshare.copy_engine import format_bytes
from contentshare.gmail.intake import IntakeReport
from contentshare.ui.context import UiContext
from contentshare.ui.request_card import OPEN_STATES, CardState, RequestCard
from contentshare.ui.widgets import Label as QLabel
from contentshare.ui.widgets import SafeMessageBox as QMessageBox
from contentshare.ui.widgets import StackBody, page_header

FILTERS: list[tuple[str, set[str] | None]] = [
    ("Open requests", OPEN_STATES),
    ("Needs review", {"NEEDS_REVIEW"}),
    ("Ready", {"READY"}),
    ("Failed", {"FAILED"}),
    ("Verified, reply not sent", {"VERIFIED"}),
    ("Everything", None),
]


def summarize_check(report: IntakeReport) -> str:
    if report.already_running:
        return "A check is already running; this one was not started."
    if report.needs_sign_in:
        return "Gmail is not connected. Open Settings → Gmail and connect."
    parts = [
        f"{report.examined} new email(s) read",
        f"{report.requests} request(s): {report.ready} ready, {report.needs_review} to review",
    ]
    if report.ignored:
        parts.append(f"{report.ignored} ignored")
    if report.errors:
        parts.append(f"{len(report.errors)} problem(s)")
    if report.stopped:
        parts.append("stopped early")
    return "Checked mail: " + "; ".join(parts) + "."


class RequestsPage(QWidget):
    def __init__(self, ctx: UiContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("page")
        self.ctx = ctx
        self._states: dict[int, CardState] = {}
        self._cards: list[RequestCard] = []
        self._refresh_pending = False

        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 26, 16)
        root.setSpacing(14)
        self._build_header(root)
        self._build_toolbar(root)

        self.scroller = QScrollArea()
        self.scroller.setWidgetResizable(True)
        self.scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body = StackBody()
        self.scroller.setWidget(self.body)
        root.addWidget(self.scroller, 1)

        self.empty = QLabel()
        self.empty.setProperty("role", "empty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        root.addWidget(self.empty, 1)
        self.refresh()

    def _build_header(self, root: QVBoxLayout) -> None:
        head = QHBoxLayout()
        head.addLayout(
            page_header("Requests", "Emails from your departments, matched to the storage server.")
        )
        head.addStretch(1)
        self.last_check = QLabel()
        self.last_check.setProperty("role", "muted")
        head.addWidget(self.last_check)
        head.addSpacing(8)
        self.check_button = QPushButton("Check now")
        self.check_button.setProperty("primary", True)
        self.check_button.clicked.connect(self.check_now)
        head.addWidget(self.check_button)
        root.addLayout(head)

        self.index_note = QLabel()
        self.index_note.setObjectName("banner")
        self.index_note.setWordWrap(True)
        root.addWidget(self.index_note)

    def _build_toolbar(self, root: QVBoxLayout) -> None:
        bar = QHBoxLayout()
        self.select_all = QCheckBox("Select all")
        self.select_all.toggled.connect(self._select_all)
        bar.addWidget(self.select_all)
        self.bulk_approve = QPushButton("Approve selected…")
        self.bulk_approve.clicked.connect(self._approve_selected)
        self.bulk_skip = QPushButton("Skip selected…")
        self.bulk_skip.clicked.connect(self._skip_selected)
        bar.addWidget(self.bulk_approve)
        bar.addWidget(self.bulk_skip)
        bar.addStretch(1)
        self.filter = QComboBox()
        self.filter.setAccessibleName("Show")
        for name, _states in FILTERS:
            self.filter.addItem(name)
        self.filter.currentIndexChanged.connect(lambda _i: self.refresh())
        bar.addWidget(self.filter)
        root.addLayout(bar)

    # ------------------------------------------------------------------ events
    def on_event(self, event: Event) -> None:
        if event.kind in ("changed", "copy_finished", "reply_sent", "reply_problem"):
            self.schedule_refresh()

    def schedule_refresh(self) -> None:
        """Several events often arrive together; rebuild once."""
        if not self._refresh_pending:
            self._refresh_pending = True
            QTimer.singleShot(50, self.refresh)

    # ------------------------------------------------------------------ listing
    def selected_states(self) -> set[str] | None:
        return FILTERS[self.filter.currentIndex()][1]

    def refresh(self) -> None:
        self._refresh_pending = False
        controller = self.ctx.controller
        wanted = self.selected_states()
        views = [v for v in controller.views() if wanted is None or v.state in wanted]
        views.sort(key=lambda v: (v.state != "NEEDS_REVIEW", v.state != "FAILED", -v.id))

        for card in self._cards:
            self.body.remove(card)
        self._cards = []
        live = {v.id for v in controller.views()}
        self._states = {k: s for k, s in self._states.items() if k in live}
        for view in views:
            state = self._states.setdefault(view.id, CardState())
            card = RequestCard(view, self.ctx, state, on_selection_changed=self._update_bulk)
            self.body.add(card)
            self._cards.append(card)

        stamp = controller.last_check()
        self.last_check.setText(
            f"Last check {stamp.astimezone():%d %b %H:%M}" if stamp else "Never checked"
        )
        self.empty.setText(
            "" if views else "Nothing to show here. Click “Check now” to look for new requests."
        )
        self.empty.setVisible(not views)
        self.scroller.setVisible(bool(views))
        summary = controller.index_summary()
        self.index_note.setVisible(summary["items"] == 0)
        self.index_note.setText(
            "The storage index is empty, so nothing can be matched yet. "
            "Open Settings → Index and refresh it."
        )
        self._update_bulk()

    def _update_bulk(self) -> None:
        chosen = [c for c in self._cards if c.state.selected]
        approvable = [c for c in chosen if c.view.state == "READY"]
        self.bulk_approve.setEnabled(bool(approvable))
        self.bulk_skip.setEnabled(bool(chosen))
        self.bulk_approve.setText(
            f"Approve selected ({len(approvable)})…" if approvable else "Approve selected…"
        )

    def _select_all(self, on: bool) -> None:
        for card in self._cards:
            card.select.setChecked(on)

    # ------------------------------------------------------------------ actions
    def check_now(self) -> None:
        self.check_button.setEnabled(False)
        self.check_button.setText("Checking…")

        def done(report: IntakeReport) -> None:
            self.check_button.setEnabled(True)
            self.check_button.setText("Check now")
            self.ctx.say(summarize_check(report))
            self.refresh()
            if report.needs_sign_in:
                QMessageBox.information(self, "Gmail", summarize_check(report))

        def failed(exc: Exception) -> None:
            self.check_button.setEnabled(True)
            self.check_button.setText("Check now")
            self.ctx.fail(self, "Could not check mail", exc)

        self.ctx.bridge.work(self.ctx.controller.check_now, done, failed)

    def _approve_selected(self) -> None:
        cards = [c for c in self._cards if c.state.selected and c.view.state == "READY"]
        genuine = [c for c in cards if c.view.verified_sender]
        skipped_unverified = len(cards) - len(genuine)
        if not genuine:
            QMessageBox.information(
                self, "Approve selected",
                "None of the selected requests can be approved in bulk. A request from an "
                "unverified sender must be approved one at a time.",
            )  # fmt: skip
            return
        total = sum(c.view.total_bytes or 0 for c in genuine)
        extra = (
            f"\n\n{skipped_unverified} request(s) from unverified senders are left out."
            if skipped_unverified
            else ""
        )
        dry = " (dry run: nothing will be copied)" if self.ctx.controller.dry_run else ""
        answer = QMessageBox.question(
            self,
            "Approve selected",
            f"Copy {len(genuine)} request(s), {format_bytes(total)} in all, to their department "
            f"folders and verify each file?{dry}{extra}",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        ids = [c.view.id for c in genuine]
        controller = self.ctx.controller

        def job() -> tuple[int, list[str]]:
            started, problems = 0, []
            for request_id in ids:
                try:
                    controller.approve(request_id, confirmed=True)
                    started += 1
                except Exception as exc:
                    problems.append(f"#{request_id}: {exc}")
            return started, problems

        def done(result: tuple[int, list[str]]) -> None:
            started, problems = result
            self.ctx.say(f"{started} request(s) approved.")
            if problems:
                QMessageBox.warning(self, "Some were not approved", "\n".join(problems))
            self.refresh()

        self.ctx.bridge.work(job, done, lambda exc: self.ctx.fail(self, "Bulk approve", exc))

    def _skip_selected(self) -> None:
        cards = [c for c in self._cards if c.state.selected]
        answer = QMessageBox.question(
            self, "Skip selected", f"Skip {len(cards)} request(s)? They stay in the history."
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        problems = []
        for card in cards:
            try:
                self.ctx.controller.skip(card.view.id)
            except Exception as exc:
                problems.append(f"#{card.view.id}: {exc}")
        if problems:
            QMessageBox.warning(self, "Some were not skipped", "\n".join(problems))
        self.refresh()

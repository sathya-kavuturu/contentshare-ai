"""A list of the mail the program looked at: when, from whom, which Gmail label, what was made
of it and why. It is the Mailbox screen, and (with only the skipped mail shown) the Skipped mail
window of the History. A skipped mail can be moved to the requests from here."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from contentshare.app.controller import ActionError, Controller
from contentshare.gmail.mailrules import CATEGORIES, mail_category
from contentshare.ui.bridge import Bridge
from contentshare.ui.timefmt import local_stamp
from contentshare.ui.widgets import Combo
from contentshare.ui.widgets import Label as QLabel
from contentshare.ui.widgets import SafeMessageBox as QMessageBox

REQUEST = "Request"
SKIPPED = "Skipped"
COLUMNS = ["Received", "From", "Subject", "Gmail label", "Result", "Why"]
SORT_ROLE = Qt.ItemDataRole.UserRole


class SortItem(QTableWidgetItem):
    """A cell that sorts by its own key (an ISO date sorts as time, not as the text shown)."""

    def __lt__(self, other: QTableWidgetItem) -> bool:
        return str(self.data(SORT_ROLE)) < str(other.data(SORT_ROLE))


def verdict_of(row: dict[str, Any]) -> str:
    """Whether the mail became a request or was left alone."""
    return REQUEST if row["lines"] else SKIPPED


def label_of(row: dict[str, Any]) -> str:
    labels = str(row.get("labels") or "").split()
    return mail_category(labels) if labels else "—"


def why_of(row: dict[str, Any]) -> str:
    if row["lines"]:
        return f"In Requests ({row['department'] or 'no department'})"
    return str(row.get("ignored_reason") or "")


def matches(row: dict[str, Any], text: str) -> bool:
    text = text.casefold().strip()
    if not text:
        return True
    haystack = " ".join(
        str(row.get(k) or "") for k in ("sender_name", "sender_email", "subject", "snippet")
    )
    return text in haystack.casefold() or text in why_of(row).casefold()


class MailView(QWidget):
    """The table, its filters and its buttons. ``only`` fixes the result shown (``SKIPPED``)."""

    def __init__(  # noqa: PLR0915 (one view, built top to bottom)
        self,
        controller: Controller,
        *,
        only: str | None = None,
        say: Callable[[str], None] | None = None,
        bridge: Bridge | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller, self.only, self._say = controller, only, say or (lambda _t: None)
        self._bridge = bridge  # with one, Gmail is read in the background
        self._rows: list[dict[str, Any]] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        bar = QHBoxLayout()
        self.search = QLineEdit(placeholderText="Search sender, subject, text…")
        self.search.setAccessibleName("Search mail")
        self.search.setMinimumWidth(240)
        self.search.textChanged.connect(lambda _t: self.refresh())
        self.label = Combo()
        self.label.setAccessibleName("Gmail label")
        self.label.addItem("All labels", None)
        for name in CATEGORIES:
            self.label.addItem(name, name)
        self.label.currentIndexChanged.connect(lambda _i: self.refresh())
        self.result = Combo()
        self.result.setAccessibleName("Result")
        self.result.addItem("Requests and skipped", None)
        self.result.addItem("Requests only", REQUEST)
        self.result.addItem("Skipped only", SKIPPED)
        self.result.currentIndexChanged.connect(lambda _i: self.refresh())
        self.result.setVisible(only is None)
        bar.addWidget(self.search, 1)
        bar.addWidget(self.label)
        bar.addWidget(self.result)
        layout.addLayout(bar)

        split = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setAccessibleName("Mail")
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setSortingEnabled(True)  # click a heading to sort (date, label, result…)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        split.addWidget(self.table)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setAccessibleName("Start of the selected mail")
        self.preview.setPlaceholderText("Select a mail to see the start of it.")
        split.addWidget(self.preview)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        layout.addWidget(split, 1)

        buttons = QHBoxLayout()
        self.move_button = QPushButton("Move to requests")
        self.move_button.setToolTip(
            "This is a request after all. It is added to the Requests, where you can add the "
            "folders and go on as usual."
        )
        self.move_button.clicked.connect(self.move_to_requests)
        self.again = QPushButton("Read again")
        self.again.setToolTip("Let the program read the selected mail once more")
        self.again.clicked.connect(self.read_again)
        self.skip_sender = QPushButton("Always skip this sender")
        self.skip_sender.setToolTip("Mail from this address is skipped without being read")
        self.skip_sender.clicked.connect(lambda: self._rule(skip=True))
        self.accept_sender = QPushButton("Always treat as requests")
        self.accept_sender.setToolTip("Mail from this address always becomes a request")
        self.accept_sender.clicked.connect(lambda: self._rule(skip=False))
        for button in (self.move_button, self.again, self.skip_sender, self.accept_sender):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.status = QLabel()  # says what is happening while Gmail is being read
        self.status.setProperty("role", "muted")
        self.status.setVisible(False)
        layout.addWidget(self.status)
        self.refresh()

    # ------------------------------------------------------------------ the list
    def visible_rows(self) -> list[dict[str, Any]]:
        label = self.label.currentData()
        wanted = self.only or self.result.currentData()
        return [
            r
            for r in self.controller.mailbox()
            if (wanted is None or verdict_of(r) == wanted)
            and (label is None or label_of(r) == label)
            and matches(r, self.search.text())
        ]

    def refresh(self) -> None:
        keep = {str(r["id"]) for r in self._selected()}
        self._rows = self.visible_rows()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self._rows))
        for i, r in enumerate(self._rows):
            cells = [
                local_stamp(r["received_at"]),
                r["sender_name"] or r["sender_email"],
                r["subject"],
                label_of(r),
                verdict_of(r),
                why_of(r),
            ]
            for col, text in enumerate(cells):
                cell = SortItem(str(text))
                cell.setToolTip(str(text))
                cell.setData(SORT_ROLE, r["received_at"] if col == 0 else str(text).casefold())
                cell.setData(Qt.ItemDataRole.UserRole + 1, r["id"])
                self.table.setItem(i, col, cell)
        self.table.setSortingEnabled(True)
        if self.table.horizontalHeader().sortIndicatorSection() < 0:
            self.table.sortByColumn(0, Qt.SortOrder.DescendingOrder)  # newest first
        for i in range(self.table.rowCount()):
            item = self.table.item(i, 0)
            if item is not None and str(item.data(Qt.ItemDataRole.UserRole + 1)) in keep:
                self.table.selectRow(i)
        self._selection_changed()

    def _selected(self) -> list[dict[str, Any]]:
        by_id = {r["id"]: r for r in self._rows}
        out = []
        for index in (
            self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        ):
            item = self.table.item(index.row(), 0)
            row = by_id.get(item.data(Qt.ItemDataRole.UserRole + 1)) if item else None
            if row is not None:
                out.append(row)
        return out

    def _selection_changed(self) -> None:
        rows = self._selected()
        skipped = [r for r in rows if verdict_of(r) == SKIPPED]
        self.move_button.setEnabled(bool(skipped))
        self.again.setEnabled(bool(skipped))
        self.skip_sender.setEnabled(bool(rows))
        self.accept_sender.setEnabled(bool(rows))
        if len(rows) != 1:
            self.preview.clear()
            return
        r = rows[0]
        self.preview.setPlainText(
            f"From: {r['sender_name'] + ' ' if r['sender_name'] else ''}<{r['sender_email']}>\n"
            f"Subject: {r['subject']}\n"
            f"Gmail label: {label_of(r)}   Result: {verdict_of(r)}\n"
            f"Why: {why_of(r)}\n\n{r.get('snippet') or '(no text kept)'}"
        )

    # ------------------------------------------------------------------ the buttons
    def _skipped_ids(self) -> list[int]:
        return [int(r["id"]) for r in self._selected() if verdict_of(r) == SKIPPED]

    def _busy(self, text: str | None) -> None:
        """Show what is going on (and keep the buttons still) until ``None``."""
        self.status.setText(text or "")
        self.status.setVisible(bool(text))
        for button in (self.move_button, self.again, self.skip_sender, self.accept_sender):
            button.setEnabled(text is None)
        if text is None:
            self._selection_changed()

    def _run(self, action: Callable[[], str], busy: str) -> None:
        """Run ``action`` (it reads Gmail) while a message says so; the window stays usable."""
        if self._bridge is None:  # no background worker (tests): just do it
            try:
                self._finished(action())
            except ActionError as exc:
                QMessageBox.warning(self, "Cannot do that", str(exc))
            return
        self._busy(busy)
        self._say(busy)

        def failed(exc: Exception) -> None:
            self._busy(None)
            text = (
                str(exc)
                if isinstance(exc, ActionError)
                else f"Something went wrong ({type(exc).__name__}). Details are in the log file."
            )
            QMessageBox.warning(self, "Cannot do that", text)

        def done(text: str) -> None:
            self._busy(None)
            self._finished(text)

        self._bridge.work(action, done, failed)

    def _finished(self, text: str) -> None:
        self._say(text)
        self.refresh()
        self.status.setText(text)  # the result stays in view, also in the dialog
        self.status.setVisible(True)

    def move_to_requests(self) -> None:
        ids = self._skipped_ids()
        if ids:
            self._run(lambda: self._move(ids), f"Moving {len(ids)} mail(s) to the requests…")

    def _move(self, ids: list[int]) -> str:
        made = self.controller.move_to_requests(ids)
        return (
            f"{len(ids)} mail(s) moved to the Requests ({len(made)} line(s)); add the folders there"
            if made
            else "Nothing was moved"
        )

    def read_again(self) -> None:
        ids = self._skipped_ids()
        if ids:
            self._run(lambda: self._again(ids), f"Reading {len(ids)} mail(s) again…")

    def _again(self, ids: list[int]) -> str:
        count = self.controller.read_mail_again(ids)
        return f"Read again: {count} became a request"

    def _rule(self, *, skip: bool) -> None:
        senders = sorted({str(r["sender_email"]) for r in self._selected()})
        for sender in senders:
            if skip:
                self.controller.always_skip_sender(sender)
            else:
                self.controller.always_accept_sender(sender)
        what = "skipped" if skip else "treated as requests"
        self._say(f"Mail from {', '.join(senders)} will always be {what}")

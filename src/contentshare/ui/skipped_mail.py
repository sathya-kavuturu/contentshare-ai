"""The mail the program looked at and left alone, and why. Any of it can be read again."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from contentshare.app.controller import Controller
from contentshare.ui.timefmt import local_stamp
from contentshare.ui.widgets import Label as QLabel

COLUMNS = ["From", "Subject", "Received", "Why it was left alone"]


class SkippedMailDialog(QDialog):
    def __init__(self, controller: Controller, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("Skipped mail")
        self.resize(900, 460)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        intro = QLabel(
            "Mail the program looked at and did not turn into a request. Once seen, a mail is "
            "not read again by itself. Select the ones to read again and they are read at the "
            "next check."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setAccessibleName("Skipped mail")
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)
        self.empty = QLabel("Nothing has been skipped.")
        layout.addWidget(self.empty)
        row = QHBoxLayout()
        self.again = QPushButton("Read the selected mail again")
        self.again.clicked.connect(self.read_again)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addStretch(1)
        row.addWidget(self.again)
        row.addWidget(close)
        layout.addLayout(row)
        self._ids: list[int] = []
        self.refresh()

    def refresh(self) -> None:
        rows = self.controller.ignored_mail()
        self._ids = [int(r["id"]) for r in rows]
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            cells = [
                r["sender_name"] or r["sender_email"],
                r["subject"],
                local_stamp(r["received_at"]),
                r["ignored_reason"] or "",
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(str(text))
                item.setToolTip(str(text))
                self.table.setItem(i, col, item)
        self.table.setVisible(bool(rows))
        self.empty.setVisible(not rows)
        self.again.setEnabled(bool(rows))

    def read_again(self) -> None:
        chosen = {i.row() for i in self.table.selectedIndexes()}
        ids = [self._ids[r] for r in chosen if r < len(self._ids)]
        if ids:
            self.controller.read_mail_again(ids)
        self.refresh()

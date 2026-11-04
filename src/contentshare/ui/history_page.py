"""History and audit trail, with CSV export."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
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

from contentshare.app.controller import Event, RequestView
from contentshare.ui.context import UiContext
from contentshare.ui.status import STATUS, style_for
from contentshare.ui.theme import current_theme
from contentshare.ui.timefmt import local_stamp
from contentshare.ui.widgets import Combo, page_header
from contentshare.ui.widgets import SafeMessageBox as QMessageBox

COLUMNS = [
    "#", "Received", "From", "Department", "Requested", "Status", "Approved by", "Files", "Size",
    "Copy time",
]  # fmt: skip
STATUS_COLUMN = COLUMNS.index("Status")


def matches(view: RequestView, text: str) -> bool:
    text = text.casefold().strip()
    if not text:
        return True
    haystack = " ".join(
        [view.sender_email, view.sender_name, view.department or "", view.subject, view.requested,
         view.source_path or "", view.destination_path or ""]
    ).casefold()  # fmt: skip
    return text in haystack


def audit_text(entries: list[dict[str, object]]) -> str:
    lines = []
    for entry in entries:
        try:
            detail = json.loads(str(entry.get("detail") or "{}"))
        except ValueError:
            detail = {}
        reason = detail.get("reason") if isinstance(detail, dict) else None
        to = f" → {detail['to']}" if isinstance(detail, dict) and detail.get("to") else ""
        lines.append(
            f"{local_stamp(entry['ts'], seconds=True)}  {entry['actor']!s:<10} {entry['event']}"
            f"{to}{(' · ' + str(reason)) if reason else ''}"
        )
    return "\n".join(lines)


class HistoryPage(QWidget):
    def __init__(self, ctx: UiContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("page")
        self.ctx = ctx
        self._views: list[RequestView] = []
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 26, 16)
        root.setSpacing(14)

        head = QHBoxLayout()
        head.addLayout(page_header("History", "Every request, newest first."))
        head.addStretch(1)
        self.search = QLineEdit(placeholderText="Search sender, subject, folder…")
        self.search.setAccessibleName("Search history")
        self.search.setMinimumWidth(260)
        self.search.textChanged.connect(lambda _t: self.refresh())
        self.state = Combo()
        self.state.setAccessibleName("Status")
        self.state.addItem("All statuses", None)
        for key, style in STATUS.items():
            self.state.addItem(style.word, key)
        self.state.currentIndexChanged.connect(lambda _i: self.refresh())
        export = QPushButton("Export CSV…")
        export.clicked.connect(self.export)
        head.addWidget(self.search)
        head.addWidget(self.state)
        head.addWidget(export)
        root.addLayout(head)

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self._build_table())
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setObjectName("audit")
        self.detail.setAccessibleName("Audit trail of the selected request")
        self.detail.setPlaceholderText("Select a request to see its audit trail.")
        split.addWidget(self.detail)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        root.addWidget(split, 1)
        self.refresh()

    def _build_table(self) -> QTableWidget:
        table = QTableWidget(0, len(COLUMNS))
        self.table = table
        table.setHorizontalHeaderLabels(COLUMNS)
        table.setAccessibleName("Request history")
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(42)
        table.setShowGrid(False)
        header = table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setHighlightSections(False)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table.itemSelectionChanged.connect(self._show_detail)
        return table

    def on_event(self, event: Event) -> None:
        if event.kind in ("changed", "copy_finished", "reply_sent"):
            self.refresh()

    def visible_views(self) -> list[RequestView]:
        wanted = self.state.currentData()
        return [
            v
            for v in self._all()
            if (wanted is None or v.state == wanted) and matches(v, self.search.text())
        ]

    def _all(self) -> list[RequestView]:
        return sorted(self.ctx.controller.views(), key=lambda v: -v.id)

    def refresh(self) -> None:
        selected = self._selected_id()
        self._views = self.visible_views()
        self.table.setRowCount(len(self._views))
        for row, v in enumerate(self._views):
            cells = [
                str(v.id), local_stamp(v.received_at),
                f"{v.sender_name or v.sender_email}", v.department or "—", v.requested,
                style_for(v.state).text, v.approved_by or "—",
                str(v.file_count) if v.file_count is not None else "",
                v.size_text, v.copy_time_text,
            ]  # fmt: skip
            tone = current_theme().tones.get(style_for(v.state).tone)
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col == STATUS_COLUMN and tone is not None:
                    item.setForeground(QColor(tone.fg))
                self.table.setItem(row, col, item)
            if v.id == selected:
                self.table.selectRow(row)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)

    def _selected_id(self) -> int | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows or rows[0].row() >= len(self._views):
            return None
        return self._views[rows[0].row()].id

    def _show_detail(self) -> None:
        request_id = self._selected_id()
        if request_id is None:
            self.detail.clear()
            return
        c = self.ctx.controller
        view = c.view(request_id)
        replies = c.replies_for(request_id)
        parts = [
            f"Subject: {view.subject}",
            f"Source: {view.source_path or '—'}",
            f"Destination: {view.destination_path or '—'}",
        ]
        if view.approved_by:
            parts.append(f"Approved by: {view.approved_by}")
        if view.copy_seconds is not None:
            speed = f" · {view.copy_speed_text} on average" if view.copy_speed_text else ""
            parts.append(f"Copy time: {view.copy_time_text}{speed}")
        if view.failure_reason:
            parts.append(f"Failure: {view.failure_reason}")
        for reply in replies:
            parts.append(
                f"Reply {reply['status']}: {reply.get('purpose')} to {reply.get('to_addr')}"
            )
        parts += ["", "Audit trail", audit_text(c.audit(request_id))]
        self.detail.setPlainText("\n".join(parts))

    def export(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(
            self, "Export history", str(Path.home() / "contentshare-history.csv"), "CSV (*.csv)"
        )
        if not path:
            return
        try:
            count = self.ctx.controller.export_history(Path(path), self.visible_views())
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        self.ctx.say(f"Exported {count} row(s) to {path}")

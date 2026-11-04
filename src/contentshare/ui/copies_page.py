"""The Active copies screen: progress, pause, resume, cancel."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from contentshare.app.controller import ActiveCopy, Event
from contentshare.copy_engine import format_bytes
from contentshare.ui.context import UiContext
from contentshare.ui.status import human_eta
from contentshare.ui.widgets import Label as QLabel
from contentshare.ui.widgets import SafeMessageBox as QMessageBox
from contentshare.ui.widgets import StackBody, page_header

STAGES = {"copy": "Copy", "verify": "Verify", "finalise": "Finalise", "done": "Done"}


def stage_line(stage: str) -> str:
    """``Copy ▸ Verify ▸ Finalise`` with the current stage in brackets."""
    names = ["copy", "verify", "finalise"]
    return " ▸ ".join(f"[{STAGES[n]}]" if n == stage else STAGES[n] for n in names)


def progress_text(item: ActiveCopy) -> str:
    p = item.progress
    if p is None:
        return "Waiting to start…"
    done = (p.copied_bytes + p.verified_bytes) / 2
    speed = f"{format_bytes(p.speed_bps)}/s" if p.speed_bps else "…"
    return (
        f"{format_bytes(done)} / {format_bytes(p.total_bytes)}  ·  {speed}  ·  "
        f"ETA {human_eta(p.eta_s)}\n{p.rel}  ·  {stage_line(p.stage)}"
    )


class CopyRow(QFrame):
    def __init__(self, item: ActiveCopy, ctx: UiContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.ctx, self.request_id = ctx, item.request_id
        self._name = item.label
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        head = QHBoxLayout()
        self.title = QLabel()
        self.title.setProperty("role", "heading")
        head.addWidget(self.title, 1)
        self.pause = QPushButton("Pause")
        self.pause.clicked.connect(self._toggle_pause)
        self.cancel = QPushButton("Cancel")
        self.cancel.setProperty("danger", True)
        self.cancel.clicked.connect(self._cancel)
        head.addWidget(self.pause)
        head.addWidget(self.cancel)
        layout.addLayout(head)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)  # the percentage is in the title, readable on any fill
        layout.addWidget(self.bar)
        self.detail = QLabel()
        self.detail.setProperty("role", "muted")
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)
        self.paused = False
        self.update_from(item)

    def update_from(self, item: ActiveCopy) -> None:
        self.paused = item.paused
        who = f" → {item.department}" if item.department else ""
        state = "⏸ Paused" if item.paused else "▶ Copying"
        self.title.setText(f"#{item.request_id}  {item.label}{who}   {state}")
        self.pause.setText("Resume" if item.paused else "Pause")
        fraction = item.progress.fraction if item.progress else 0.0
        self.bar.setValue(int(fraction * 1000))
        self.title.setText(f"{self.title.text()}   {fraction:.0%}")
        self.detail.setText(progress_text(item))

    def _toggle_pause(self) -> None:
        controller = self.ctx.controller
        (controller.resume if self.paused else controller.pause)(self.request_id)

    def _cancel(self) -> None:
        answer = QMessageBox.question(
            self,
            "Cancel copy",
            f"Stop copying {self._name}?\n\nFiles already verified are kept and the copy can be "
            "retried later; nothing is deleted from the source.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,  # Enter must never cancel a copy by accident
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.ctx.controller.cancel(self.request_id)


class CopiesPage(QWidget):
    def __init__(self, ctx: UiContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("page")
        self.ctx = ctx
        self._rows: dict[int, CopyRow] = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 26, 16)
        root.setSpacing(14)
        root.addLayout(page_header("Active copies", "Copies that are running right now."))
        self.empty = QLabel("No copies are running.\nApproved requests show their progress here.")
        self.empty.setProperty("role", "empty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body = StackBody()
        scroll.setWidget(self.body)
        self._scroll = scroll
        root.addWidget(scroll, 1)
        root.addWidget(self.empty, 1)
        self._timer = QTimer(self)  # progress arrives often; draw at most 4 times a second
        self._timer.setSingleShot(True)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self.refresh)
        self.refresh()

    def on_event(self, event: Event) -> None:
        if (
            event.kind in ("copy_progress", "changed", "copy_finished")
            and not self._timer.isActive()
        ):
            self._timer.start()

    def refresh(self) -> None:
        active = {a.request_id: a for a in self.ctx.controller.active_copies()}
        for request_id in [r for r in self._rows if r not in active]:
            self.body.remove(self._rows.pop(request_id))
        for request_id, item in active.items():
            if request_id in self._rows:
                self._rows[request_id].update_from(item)
            else:
                row = CopyRow(item, self.ctx)
                self._rows[request_id] = row
                self.body.add(row)
        self.empty.setVisible(not active)
        self._scroll.setVisible(bool(active))

    def count(self) -> int:
        return len(self._rows)

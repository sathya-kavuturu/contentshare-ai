"""The mail the program looked at and did not turn into a request, and why. Any of it can be
moved to the requests, or read again."""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from contentshare.app.controller import Controller
from contentshare.ui.bridge import Bridge
from contentshare.ui.mail_view import SKIPPED, MailView
from contentshare.ui.widgets import Label as QLabel


class SkippedMailDialog(QDialog):
    def __init__(
        self, controller: Controller, parent: QWidget | None = None, *, bridge: Bridge | None = None
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("Skipped mail")
        self.resize(1000, 560)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        intro = QLabel(
            "Mail the program looked at and did not take for a request, with the reason. If one "
            "is a request after all, select it and choose Move to requests. Once seen, a mail is "
            "not read again by itself."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.view = MailView(controller, only=SKIPPED, bridge=bridge)
        layout.addWidget(self.view, 1)
        row = QHBoxLayout()
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addStretch(1)
        row.addWidget(close)
        layout.addLayout(row)

"""Every mail the program looked at, in one list: sort by date or Gmail label, filter by what
became of it, and move a skipped mail to the requests."""

from __future__ import annotations

from PySide6.QtWidgets import QVBoxLayout, QWidget

from contentshare.app.controller import Event
from contentshare.ui.context import UiContext
from contentshare.ui.mail_view import MailView
from contentshare.ui.widgets import page_header


class MailboxPage(QWidget):
    def __init__(self, ctx: UiContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("page")
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 26, 16)
        root.setSpacing(14)
        root.addLayout(
            page_header(
                "Mailbox",
                "Every mail the program looked at, in every Gmail folder, and what became of it.",
            )
        )
        self.view = MailView(ctx.controller, say=ctx.say, bridge=ctx.bridge)
        root.addWidget(self.view, 1)

    def refresh(self) -> None:
        self.view.refresh()

    def on_event(self, event: Event) -> None:
        if event.kind in ("changed", "reply_sent"):
            self.refresh()

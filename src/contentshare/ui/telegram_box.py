"""Settings: alerts to a Telegram group when new requests wait for approval."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from contentshare.app.telegram import Chat, TelegramError, valid_chat, valid_token
from contentshare.config.models import AppConfig
from contentshare.ui.context import UiContext
from contentshare.ui.widgets import Label as QLabel
from contentshare.ui.widgets import SafeMessageBox as QMessageBox

HELP = (
    "1. In Telegram, open @BotFather, send /newbot and copy the token it gives you.\n"
    "2. Add the new bot to your group, then write any message in the group.\n"
    "3. Paste the token here and press “Find my group”. Then press “Send a test message”.\n"
    "Only counts are sent (for example “3 new requests waiting for approval”), never an email "
    "subject, sender or program name. The token is kept in Windows Credential Manager."
)
TEST_TEXT = "ContentShare: this is a test message. New-request alerts will arrive here."


class TelegramBox(QWidget):
    def __init__(self, ctx: UiContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.enabled = QCheckBox("Tell a Telegram group when new requests wait for approval")
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setAccessibleName("Telegram bot token")
        self.chat = QLineEdit()
        self.chat.setAccessibleName("Telegram group number")
        self.chat.setPlaceholderText("-1001234567890")
        self.find_group = QPushButton("Find my group")
        self.test = QPushButton("Send a test message")
        self.forget = QPushButton("Forget the token")
        self.find_group.clicked.connect(self._find)
        self.test.clicked.connect(self._test)
        self.forget.clicked.connect(self._forget)
        self.note = QLabel(HELP)
        self.note.setWordWrap(True)
        self.note.setProperty("role", "small")

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Bot token", self.token)
        group_row = QHBoxLayout()
        group_row.addWidget(self.chat, 1)
        group_row.addWidget(self.find_group)
        form.addRow("Group number", group_row)
        buttons = QHBoxLayout()
        buttons.addWidget(self.test)
        buttons.addWidget(self.forget)
        buttons.addStretch(1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        outer.addWidget(self.enabled)
        outer.addLayout(form)
        outer.addLayout(buttons)
        outer.addWidget(self.note)
        self.enabled.toggled.connect(self._enable_fields)
        self._enable_fields(False)

    # ------------------------------------------------------------------ settings values
    def load(self, config: AppConfig) -> None:
        self.enabled.setChecked(config.telegram.enabled)
        self.chat.setText(config.telegram.chat_id)
        self.token.clear()
        self._show_token_state()

    def values(self) -> dict[str, object]:
        return {"enabled": self.enabled.isChecked(), "chat_id": self.chat.text().strip()}

    def commit(self) -> None:
        """Store a newly typed token and check the alerts can work. Raises ``TelegramError``."""
        notifier = self.ctx.controller.telegram
        typed = self.token.text().strip()
        if typed:
            notifier.save_token(typed)
            self.token.clear()
        if self.enabled.isChecked():
            if not valid_chat(self.chat.text().strip()):
                raise TelegramError("Enter the group number, or turn the Telegram alerts off.")
            if not notifier.has_token():
                raise TelegramError("Paste the bot token, or turn the Telegram alerts off.")
        self._show_token_state()

    # ------------------------------------------------------------------ helpers
    def _enable_fields(self, on: bool) -> None:
        for widget in (self.token, self.chat, self.find_group, self.test, self.forget):
            widget.setEnabled(on)

    def _show_token_state(self) -> None:
        # No look in the credential store here (it can be slow): the text covers both cases.
        self.token.setPlaceholderText(
            "Paste the token from @BotFather (a token saved earlier is kept if this is empty)"
        )

    def _typed_token(self) -> str | None:
        typed = self.token.text().strip()
        if typed and not valid_token(typed):
            QMessageBox.warning(
                self,
                "Telegram",
                "That does not look like a bot token. It is the long text from @BotFather, "
                "like 123456789:AA...",
            )
            raise TelegramError("bad token")
        return typed or None

    def _run(self, job: Callable[[], Any], done: Callable[[Any], object]) -> None:
        def failed(exc: Exception) -> None:
            self._busy(False)
            text = (
                str(exc)
                if isinstance(exc, TelegramError)
                else "The Telegram alert could not be sent."
            )
            QMessageBox.warning(self, "Telegram", text)

        def finished(result: object) -> None:
            self._busy(False)
            done(result)

        self._busy(True)
        self.ctx.bridge.work(job, finished, failed)

    def _busy(self, busy: bool) -> None:
        for button in (self.find_group, self.test, self.forget):
            button.setEnabled(not busy and self.enabled.isChecked())

    # ------------------------------------------------------------------ buttons
    def _test(self) -> None:
        notifier = self.ctx.controller.telegram
        chat = self.chat.text().strip()
        try:
            token = self._typed_token()
        except TelegramError:
            return
        if not valid_chat(chat):
            QMessageBox.information(self, "Telegram", "Enter the group number first.")
            return
        self._run(
            lambda: notifier.send(chat, TEST_TEXT, token=token),
            lambda _r: QMessageBox.information(
                self, "Telegram", "The test message was sent. Look for it in the group."
            ),
        )

    def _find(self) -> None:
        notifier = self.ctx.controller.telegram
        try:
            token = self._typed_token()
        except TelegramError:
            return
        self._run(lambda: notifier.recent_chats(token=token), self._found)

    def _found(self, chats: list[Chat]) -> None:
        if not chats:
            QMessageBox.information(
                self,
                "Telegram",
                "The bot has not seen a group yet. Add the bot to the group, write a message "
                "there, then press “Find my group” again.",
            )
            return
        if len(chats) == 1:
            self.chat.setText(chats[0].chat_id)
            return
        names = [f"{c.title}  ({c.chat_id})" for c in chats]
        picked, ok = QInputDialog.getItem(self, "Telegram", "Which group?", names, 0, False)
        if ok:
            self.chat.setText(chats[names.index(picked)].chat_id)

    def _forget(self) -> None:
        answer = QMessageBox.question(
            self, "Telegram", "Remove the saved bot token? Alerts stop until you add one again."
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.ctx.controller.telegram.forget_token()
            self._show_token_state()

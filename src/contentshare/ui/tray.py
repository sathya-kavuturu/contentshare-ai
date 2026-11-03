"""The notification-area icon: open, check now, quit, and pop-up messages."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon, QWidget

from contentshare.app.notifications import Toast
from contentshare.ui.icons import app_icon


class Tray(QObject):
    open_requested = Signal()
    check_requested = Signal()
    quit_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.icon = QSystemTrayIcon(app_icon(), self)
        self.icon.setToolTip("ContentShare")
        self.menu = QMenu()
        self.open_action = QAction("Open ContentShare", self.menu)
        self.check_action = QAction("Check for requests now", self.menu)
        self.status_action = QAction("", self.menu)
        self.status_action.setEnabled(False)
        self.quit_action = QAction("Quit", self.menu)
        for action in (self.open_action, self.check_action):
            self.menu.addAction(action)
        self.menu.addSeparator()
        self.menu.addAction(self.status_action)
        self.menu.addSeparator()
        self.menu.addAction(self.quit_action)
        self.open_action.triggered.connect(self.open_requested)
        self.check_action.triggered.connect(self.check_requested)
        self.quit_action.triggered.connect(self.quit_requested)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)
        self.icon.messageClicked.connect(self.open_requested)

    @staticmethod
    def available() -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    def show(self) -> None:
        self.icon.show()

    def hide(self) -> None:
        self.icon.hide()

    def visible(self) -> bool:
        return self.icon.isVisible()

    def set_status(self, text: str, *, waiting: int = 0) -> None:
        """One menu line (for example the next check time) and the tooltip. ``waiting`` puts a
        dot on the icon so a glance shows whether anything needs the user."""
        self.status_action.setText(text)
        tip = "ContentShare"
        if waiting:
            tip += f": {waiting} waiting for review"
        self.icon.setToolTip(f"{tip}\n{text}" if text else tip)
        self.icon.setIcon(app_icon(attention=waiting > 0))

    def notify(self, toast: Toast) -> None:
        icon = (
            QSystemTrayIcon.MessageIcon.Warning
            if toast.urgent
            else QSystemTrayIcon.MessageIcon.Information
        )
        self.icon.showMessage(toast.title, toast.text, icon, 12000 if toast.urgent else 7000)

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.open_requested.emit()

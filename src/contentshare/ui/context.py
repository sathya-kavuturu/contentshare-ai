"""What every screen needs, bundled so pages and cards take one argument."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtWidgets import QWidget

from contentshare.app.controller import ActionError, Controller
from contentshare.app.startup import StartAtLogin
from contentshare.ui.bridge import Bridge
from contentshare.ui.widgets import SafeMessageBox as QMessageBox


@dataclass
class UiContext:
    controller: Controller
    bridge: Bridge
    say: Callable[[str], None]  # message for the status line
    # The main window: parent for message boxes shown after the widget that started a
    # background job has already been rebuilt.
    owner: QWidget | None = None
    startup: StartAtLogin | None = None  # None where Windows start-up cannot be changed (tests)

    def fail(self, parent: QWidget | None, title: str, exc: Exception) -> None:
        """Show an error. ``ActionError`` texts are written for the user; anything else is a
        bug, so only its type is shown (details are in the log)."""
        text = (
            str(exc)
            if isinstance(exc, ActionError)
            else f"Something went wrong ({type(exc).__name__}). Details are in the log file."
        )
        QMessageBox.warning(parent, title, text)

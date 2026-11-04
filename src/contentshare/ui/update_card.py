"""The "a new version is available" box at the bottom left of the window.

States, and the buttons each one shows:

* available    — "Version X is available"            [Download] [Not now]
* downloading  — a progress bar                       [Cancel]
* ready        — "Version X is ready to install"     [Update] [Cancel]
* message      — "You have the latest version", or what went wrong   [OK]

Nothing is installed until the user clicks Update.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from enum import Enum
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame, QProgressBar, QPushButton, QVBoxLayout, QWidget

from contentshare.app.updates import UpdateCancelledError, UpdateError, UpdateInfo, Updater
from contentshare.ui.bridge import Bridge
from contentshare.ui.widgets import Label as QLabel

log = logging.getLogger(__name__)


class State(Enum):
    HIDDEN = "hidden"
    AVAILABLE = "available"
    DOWNLOADING = "downloading"
    READY = "ready"
    MESSAGE = "message"


def megabytes(size: int, digits: int = 0) -> str:
    return f"{size / (1024 * 1024):.{digits}f} MB"


class UpdateCard(QFrame):
    found = Signal(str)  # the version of a newer release, once per check
    _progress = Signal(int, int)  # emitted from the download thread, delivered on the GUI thread

    def __init__(
        self,
        updater: Updater,
        bridge: Bridge,
        busy: Callable[[], bool],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("updatecard")
        self.updater, self.bridge, self._busy = updater, bridge, busy
        self.state = State.HIDDEN
        self.info: UpdateInfo | None = None
        self.installer: Path | None = None
        self._generation = 0  # a late answer from a cancelled or older job is ignored
        self._cancel = threading.Event()

        self.title = QLabel("")
        self.title.setProperty("role", "heading")
        self.title.setWordWrap(True)
        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setProperty("role", "muted")
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)  # the box is narrow; the size line says how far it is
        self.primary = QPushButton("")
        self.primary.setProperty("primary", True)
        self.secondary = QPushButton("")
        self.primary.clicked.connect(self._on_primary)
        self.secondary.clicked.connect(self._on_secondary)
        # One above the other: the sidebar is narrow, and each button needs its full width.
        buttons = QVBoxLayout()
        buttons.setSpacing(6)
        buttons.addWidget(self.primary)
        buttons.addWidget(self.secondary)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)
        layout.addWidget(self.title)
        layout.addWidget(self.detail)
        layout.addWidget(self.bar)
        layout.addLayout(buttons)
        self._progress.connect(self._on_progress)
        self._show(State.HIDDEN)

    # ------------------------------------------------------------------ checking
    def check(self, *, manual: bool = False) -> None:
        """Look for a newer version in the background. ``manual`` also reports "up to date"
        and problems; an automatic check stays quiet unless there is something to offer."""
        if self.state in (State.DOWNLOADING, State.READY):
            return  # already dealing with one
        self._generation += 1
        generation = self._generation
        if manual:
            self._message("Checking for updates…", "")

        def done(info: UpdateInfo | None) -> None:
            if generation != self._generation:
                return
            if info is None:
                if manual:
                    self._message("You have the latest version.", f"ContentShare {self._current()}")
                else:
                    self._show(State.HIDDEN)
                return
            self.info = info
            self._show(State.AVAILABLE)
            self.found.emit(info.version)

        def failed(exc: Exception) -> None:
            if generation != self._generation:
                return
            if manual:
                self._message("Could not check for updates.", self._reason(exc))
            else:
                self._show(State.HIDDEN)  # a failed automatic check is not worth a box

        self.bridge.work(self.updater.check, done, failed)

    # ------------------------------------------------------------------ buttons
    def _on_primary(self) -> None:
        if self.state is State.AVAILABLE:
            self.start_download()
        elif self.state is State.READY:
            self.install()
        else:  # message
            self._show(State.HIDDEN)

    def _on_secondary(self) -> None:
        if self.state is State.DOWNLOADING:
            self._cancel.set()
            self._generation += 1
        self._show(State.HIDDEN)

    def start_download(self) -> None:
        if self.info is None:
            return
        info = self.info
        self._cancel = threading.Event()
        cancel = self._cancel
        self._generation += 1
        generation = self._generation
        self._show(State.DOWNLOADING)

        def job() -> Path:
            return self.updater.download(info, self._progress.emit, cancel.is_set)

        def done(path: Path) -> None:
            if generation != self._generation:
                return
            self.installer = path
            self._show(State.READY)

        def failed(exc: Exception) -> None:
            if generation != self._generation or isinstance(exc, UpdateCancelledError):
                return
            self._message("The update was not downloaded.", self._reason(exc))

        self.bridge.work(job, done, failed)

    def install(self) -> None:
        if self.installer is None:
            return
        if self._busy():
            self.detail.setText("Copies are running. Update when they have finished.")
            return
        try:
            self.updater.install(self.installer)
        except UpdateError as exc:
            self._message("The update could not be started.", str(exc))
            return
        self._message(
            "The installer is starting.",
            "ContentShare closes itself while it updates, then you can start it again.",
        )

    # ------------------------------------------------------------------ display
    def _on_progress(self, received: int, total: int) -> None:
        if self.state is not State.DOWNLOADING:
            return
        if received <= 0:
            self._connecting()  # the checksum and the connection come before the first byte
            return
        self.bar.setRange(0, 100)
        self.bar.setValue(min(100, int(received * 100 / total)) if total else 0)
        self.detail.setText(f"{megabytes(received, 1)} of {megabytes(total, 1)}")

    def _connecting(self) -> None:
        self.bar.setRange(0, 0)  # a moving bar: it is working, there is just no percentage yet
        self.detail.setText("Connecting to the update server…")

    def _current(self) -> str:
        return ".".join(str(n) for n in self.updater.current)

    @staticmethod
    def _reason(exc: Exception) -> str:
        if isinstance(exc, UpdateError):
            return str(exc)
        log.warning("unexpected update problem: %s", type(exc).__name__)
        return f"Something went wrong ({type(exc).__name__}). Details are in the log file."

    def _message(self, title: str, detail: str) -> None:
        self.title.setText(title)
        self.detail.setText(detail)
        self._show(State.MESSAGE, keep_text=True)

    def _show(self, state: State, *, keep_text: bool = False) -> None:
        self.state = state
        version = self.info.version if self.info else ""
        if not keep_text:
            if state is State.AVAILABLE and self.info:
                self.title.setText(f"Version {version} is available")
                self.detail.setText(
                    f"You have {self._current()}. Download size {megabytes(self.info.size)}."
                )
            elif state is State.DOWNLOADING:
                self.title.setText(f"Downloading version {version}…")
                self._connecting()
            elif state is State.READY:
                self.title.setText(f"Version {version} is ready to install")
                self.detail.setText("It was checked against the published checksum.")
        self.bar.setVisible(state is State.DOWNLOADING)
        self.detail.setVisible(bool(self.detail.text()) or state is State.DOWNLOADING)
        labels = {
            State.AVAILABLE: ("Download", "Not now"),
            State.DOWNLOADING: ("", "Cancel"),
            State.READY: ("Update", "Cancel"),
            State.MESSAGE: ("OK", ""),
        }
        first, second = labels.get(state, ("", ""))
        self.primary.setText(first)
        self.primary.setVisible(bool(first))
        self.secondary.setText(second)
        self.secondary.setVisible(bool(second))
        self.setVisible(state is not State.HIDDEN)

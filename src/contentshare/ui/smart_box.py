"""Settings → Smart reading: read requests from senders who are in no department with a small
language model that runs on this PC."""

from __future__ import annotations

import threading
from typing import Any

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from contentshare.app.controller import ActionError
from contentshare.config.models import AppConfig
from contentshare.smart.catalog import MODELS
from contentshare.smart.schema import Reading
from contentshare.ui.context import UiContext
from contentshare.ui.widgets import Combo
from contentshare.ui.widgets import Label as QLabel
from contentshare.ui.widgets import SafeMessageBox as QMessageBox

HELP = (
    "When this is on, ContentShare also reads mail from people who are in no department. A small "
    "language model on this PC (nothing is sent anywhere) finds the folder names in the email, "
    "and each name is looked up on the storage server. Everything it finds waits for you: pick "
    "the department, check the names, then approve. The model files are downloaded once "
    "(they are checked before use) and are open-source: Qwen3 (Apache License 2.0) run by "
    "llama.cpp (MIT)."
)


def _size(bytes_: int) -> str:
    return (
        f"{bytes_ / 1_000_000_000:.1f} GB"
        if bytes_ >= 100_000_000
        else f"{bytes_ / 1_000_000:.0f} MB"
    )


def describe(reading: Reading) -> str:
    """What the model made of a sample email, in words."""
    if not reading.is_request:
        why = f" ({reading.reason})" if reading.reason else ""
        return f"Not a content request{why}."
    names = "\n".join(f"  • {name}" for name in reading.folders)
    return f"A content request. Folders found:\n{names}\n(confidence {reading.confidence:.0%})"


class TryDialog(QDialog):
    """Paste an email, see what the model finds. Nothing is saved or sent."""

    def __init__(self, ctx: UiContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle("Try the smart reader")
        self.resize(640, 520)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel("Paste the subject and text of an email. Nothing is saved or sent.")
        )
        self.subject = QLineEdit(placeholderText="Subject")
        self.subject.setAccessibleName("Subject")
        self.body = QPlainTextEdit()
        self.body.setAccessibleName("Email text")
        self.body.setPlaceholderText("Email text")
        self.outcome = QLabel()
        self.outcome.setWordWrap(True)
        self.go = QPushButton("Read it")
        self.go.setProperty("primary", True)
        self.go.clicked.connect(self._read)
        layout.addWidget(self.subject)
        layout.addWidget(self.body, 1)
        layout.addWidget(self.outcome)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.go)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        row.addWidget(close)
        layout.addLayout(row)

    def _read(self) -> None:
        subject, body = self.subject.text(), self.body.toPlainText()
        self.go.setEnabled(False)
        self.outcome.setText(
            "Reading… the first time the model has to start, which can take a minute."
        )
        controller = self.ctx.controller

        def done(reading: Reading) -> None:
            self.go.setEnabled(True)
            self.outcome.setText(describe(reading))

        def failed(exc: Exception) -> None:
            self.go.setEnabled(True)
            self.outcome.setText(
                str(exc) if isinstance(exc, ActionError) else "It could not read that."
            )

        self.ctx.bridge.work(lambda: controller.smart_test(subject, body), done, failed)


class SmartBox(QWidget):
    def __init__(  # noqa: PLR0915 (one box, built top to bottom)
        self, ctx: UiContext, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._stop = threading.Event()
        self._installing = False
        self._starting = False
        self.enabled = QCheckBox("Read emails from senders that are not in a department")
        self.model = Combo()
        self.model.setAccessibleName("Model")
        for key, choice in MODELS.items():
            self.model.addItem(choice.label, key)
        self.gpu = QCheckBox("Use the graphics card (faster; needs one with 2 GB or more)")
        self.learn = QCheckBox("Learn from my approvals and rejections")
        self.per_check = QSpinBox()
        self.per_check.setRange(1, 100)
        self.per_check.setMaximumWidth(160)
        self.per_check.setSuffix(" emails")
        self.keep = QSpinBox()
        self.keep.setRange(1, 120)
        self.keep.setMaximumWidth(160)
        self.keep.setSuffix(" minutes")

        self.state = QLabel()
        self.state.setWordWrap(True)
        self.download = QPushButton("Download…")
        self.download.clicked.connect(self._download)
        self.stop = QPushButton("Stop")
        self.stop.clicked.connect(self._stop.set)
        self.stop.setVisible(False)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.run_button = QPushButton("Start the model now")
        self.run_button.clicked.connect(self._toggle_run)
        self.try_button = QPushButton("Try it on an email…")
        self.try_button.clicked.connect(self._try)
        self.forget = QPushButton("Forget what it learned")
        self.forget.clicked.connect(self._forget)
        self.note = QLabel(HELP)
        self.note.setWordWrap(True)
        self.note.setProperty("role", "small")

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Model", self.model)
        form.addRow("", self.gpu)
        form.addRow("", self.learn)
        form.addRow("Read at most", self.per_check)
        form.addRow("Stop the model after", self.keep)
        status_row = QHBoxLayout()
        status_row.addWidget(self.state, 1)
        status_row.addWidget(self.download)
        status_row.addWidget(self.stop)
        buttons = QHBoxLayout()
        buttons.addWidget(self.run_button)
        buttons.addWidget(self.try_button)
        buttons.addWidget(self.forget)
        buttons.addStretch(1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)
        outer.addWidget(self.enabled)
        outer.addLayout(form)
        outer.addLayout(status_row)
        outer.addWidget(self.progress)
        outer.addLayout(buttons)
        outer.addWidget(self.note)

    # ------------------------------------------------------------------ settings values
    def load(self, config: AppConfig) -> None:
        smart = config.smart
        self.enabled.setChecked(smart.enabled)
        self.model.setCurrentIndex(max(0, self.model.findData(smart.model)))
        self.gpu.setChecked(smart.use_graphics_card)
        self.learn.setChecked(smart.learn)
        self.per_check.setValue(smart.max_per_check)
        self.keep.setValue(smart.keep_loaded_minutes)
        self.refresh_status()

    def values(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled.isChecked(),
            "model": str(self.model.currentData() or "recommended"),
            "use_graphics_card": self.gpu.isChecked(),
            "learn": self.learn.isChecked(),
            "max_per_check": self.per_check.value(),
            "keep_loaded_minutes": self.keep.value(),
        }

    def _unsaved(self) -> bool:
        saved = self.ctx.controller.config.smart.model_dump()
        return any(self.values()[k] != saved[k] for k in ("model", "use_graphics_card"))

    # ------------------------------------------------------------------ status and download
    def refresh_status(self) -> None:
        status = self.ctx.controller.smart_status()
        running = self.ctx.controller.smart_running()
        self.run_button.setText("Stop the model" if running else "Start the model now")
        self.run_button.setEnabled(status.ready and not self._starting)
        if status.ready:
            now = "running now" if running else "not running; it starts by itself when needed"
            self.state.setText(f"Installed: {status.model_label} ({now}).")
        else:
            self.state.setText(
                f"Not installed. Download needed: about {_size(status.missing_bytes)} "
                f"({status.model_label})."
            )
        self.download.setEnabled(not status.ready and not self._installing)
        self.try_button.setEnabled(status.ready)

    def _download(self) -> None:
        if self._unsaved():
            QMessageBox.information(
                self, "Save first", "Press “Save settings” first, then download the model."
            )
            return
        controller = self.ctx.controller
        self._installing = True
        self._stop.clear()
        self.download.setEnabled(False)
        self.stop.setVisible(True)
        self.progress.setVisible(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        bridge = self.ctx.bridge

        def progress(name: str, done: int, total: int) -> None:
            percent = int(done * 100 / total) if total else 0
            bridge.post(lambda: self._show_progress(name, percent))

        def finished(_result: object) -> None:
            self._end_install()
            self.state.setText("Downloaded and checked.")
            self.refresh_status()
            self.ctx.say("The smart reader is installed")

        def failed(exc: Exception) -> None:
            self._end_install()
            self.ctx.fail(self, "The download did not finish", exc)
            self.refresh_status()

        bridge.work(lambda: controller.smart_install(progress, self._stop.is_set), finished, failed)

    def _show_progress(self, name: str, percent: int) -> None:
        self.progress.setValue(percent)
        self.state.setText(f"Downloading {name}… {percent}%")

    def _end_install(self) -> None:
        self._installing = False
        self.stop.setVisible(False)
        self.progress.setVisible(False)

    def _toggle_run(self) -> None:
        controller = self.ctx.controller
        if controller.smart_running():
            controller.smart_stop()
            self.refresh_status()
            return
        self._starting = True
        self.run_button.setEnabled(False)
        self.state.setText("Starting the model… this can take a minute.")

        def done(_result: object) -> None:
            self._starting = False
            self.refresh_status()
            self.ctx.say("The model is running")

        def failed(exc: Exception) -> None:
            self._starting = False
            self.refresh_status()
            self.ctx.fail(self, "The model did not start", exc)

        self.ctx.bridge.work(controller.smart_start, done, failed)

    def _try(self) -> None:
        TryDialog(self.ctx, self).exec()

    def _forget(self) -> None:
        count = self.ctx.controller.smart_forget_examples()
        self.ctx.say(f"Forgot {count} learned example(s)")

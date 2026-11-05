"""Settings → Smart reading: read requests from senders who are in no department with a small
language model that runs on this PC."""

from __future__ import annotations

import json
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
from contentshare.smart.prompt import (
    MAX_BODY_CHARS,
    MAX_EXAMPLES,
    REQUEST_HINTS,
    SYSTEM,
    build_messages,
)
from contentshare.smart.samples import SAMPLES
from contentshare.smart.schema import ANSWER_SCHEMA, Reading
from contentshare.ui.context import UiContext
from contentshare.ui.widgets import Combo
from contentshare.ui.widgets import Label as QLabel
from contentshare.ui.widgets import SafeMessageBox as QMessageBox

HELP = (
    "A small language model on this PC (nothing is sent anywhere) reads an email and finds the "
    "folder names in it; each name is then looked up on the storage server. It is used in two "
    "places: for mail from people who are in no department (the second tick below), and for "
    "any department whose own tick is set (Settings → Departments → Local model), when the "
    "usual reading cannot find exact folder names. Whatever it finds waits for your approval. "
    "The model files are downloaded once (they are checked before use) and are open-source: "
    "Qwen3 (Apache License 2.0) run by llama.cpp (MIT)."
)


def prompt_text() -> str:
    """Everything the model is sent for one email, in the order it is sent."""
    sample = build_messages("<the subject>", "<the text of the email>", [])
    parts = [
        "1. INSTRUCTIONS (always sent first)",
        SYSTEM,
        "",
        "2. THE EMAIL (sent next, as the user's message)",
        sample[-1]["content"],
        f"(the text is cut at {MAX_BODY_CHARS} characters; quoted replies, signatures and "
        "control characters are removed first; any <email> tags written inside the email are "
        "deleted)",
        "",
        "3. EXAMPLES (only when “Learn from my approvals and rejections” is ticked)",
        f"Up to {MAX_EXAMPLES} earlier emails that look like this one are put between 1 and 2, "
        "each followed by the answer you decided on, in the same JSON shape as below.",
        "",
        "4. THE ANSWER SHAPE (enforced by the model server)",
        json.dumps(ANSWER_SCHEMA, indent=2),
        "",
        "BEFORE THE MODEL IS ASKED",
        "An email is not sent to the model at all unless it contains one of these words: "
        + ", ".join(REQUEST_HINTS)
        + ".",
    ]
    return "\n".join(parts)


class PromptDialog(QDialog):
    """Shows the instructions and layout sent to the model. Read-only."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("What is sent to the model")
        self.resize(720, 640)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "This is exactly what the model on this PC is given for each email. "
                "It cannot be changed here."
            )
        )
        text = QPlainTextEdit(prompt_text())
        text.setReadOnly(True)
        text.setAccessibleName("Prompt")
        layout.addWidget(text, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        layout.addLayout(row)


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
        text = f"Not a content request{why}."
    else:
        names = "\n".join(f"  • {name}" for name in reading.folders) or "  (no names)"
        text = f"A content request. Folders found:\n{names}\n(confidence {reading.confidence:.0%})"
    if reading.dropped:
        text += "\nLeft out (not written in the email, or not a plain name): " + "; ".join(
            reading.dropped
        )
    return text


def timing(reading: Reading) -> str:
    if reading.seconds > 0:
        return f"The model answered in {reading.seconds:.1f} s."
    return "The model was not asked: the email has none of the words that ask for something."


class TryDialog(QDialog):
    """Choose a sample email (or paste one), edit it, see what the model answers and how long it
    took. Nothing is saved or sent."""

    def __init__(self, ctx: UiContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle("Try the smart reader")
        self.resize(720, 760)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Choose a sample email, change the text if you like, and press Read it. "
                "Nothing is saved or sent."
            )
        )
        self.template = Combo()
        self.template.setAccessibleName("Sample email")
        self.template.addItem("Your own email (empty)")
        for sample in SAMPLES:
            self.template.addItem(sample.title)
        self.template.currentIndexChanged.connect(self._choose)
        self.expect = QLabel()
        self.expect.setWordWrap(True)
        self.subject = QLineEdit(placeholderText="Subject")
        self.subject.setAccessibleName("Subject")
        self.body = QPlainTextEdit()
        self.body.setAccessibleName("Email text")
        self.body.setPlaceholderText("Email text")
        self.outcome = QLabel()
        self.outcome.setWordWrap(True)
        self.answer = QPlainTextEdit()
        self.answer.setReadOnly(True)
        self.answer.setAccessibleName("The model's own answer")
        self.answer.setPlaceholderText("The model's own answer appears here")
        self.answer.setMaximumHeight(130)
        self.go = QPushButton("Read it")
        self.go.setProperty("primary", True)
        self.go.clicked.connect(self._read)
        form = QFormLayout()
        form.addRow("Sample", self.template)
        layout.addLayout(form)
        layout.addWidget(self.expect)
        layout.addWidget(self.subject)
        layout.addWidget(self.body, 1)
        layout.addWidget(self.outcome)
        layout.addWidget(self.answer)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.go)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        row.addWidget(close)
        layout.addLayout(row)
        self._choose(1)  # the first sample, so there is something to read at once
        self.template.setCurrentIndex(1)

    def _choose(self, index: int) -> None:
        if index <= 0:
            self.expect.setText("")
            self.subject.clear()
            self.body.clear()
            return
        sample = SAMPLES[index - 1]
        self.expect.setText(f"A good answer: {sample.expect}")
        self.subject.setText(sample.subject)
        self.body.setPlainText(sample.body)
        self.outcome.setText("")
        self.answer.clear()

    def _read(self) -> None:
        subject, body = self.subject.text(), self.body.toPlainText()
        self.go.setEnabled(False)
        self.answer.clear()
        self.outcome.setText(
            "Reading… the first time the model has to start, which can take a minute."
        )
        controller = self.ctx.controller

        def done(reading: Reading) -> None:
            self.go.setEnabled(True)
            self.outcome.setText(f"{describe(reading)}\n{timing(reading)}")
            self.answer.setPlainText(reading.answer)

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
        self.enabled = QCheckBox("Use the local model (master switch: off = it is never started)")
        self.unlisted = QCheckBox("Also read emails from senders that are not in any department")
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
        self.progress.setTextVisible(True)
        self.progress.setFormat("%p%")
        # The app-wide thin bar (8 px) has no room for its text; this one is tall enough.
        self.progress.setStyleSheet(
            "QProgressBar { min-height: 22px; max-height: 22px; border-radius: 6px; "
            "text-align: center; font-weight: 600; } "
            "QProgressBar::chunk { border-radius: 6px; }"
        )
        self.run_button = QPushButton("Start the model now")
        self.run_button.clicked.connect(self._toggle_run)
        self.prompt_button = QPushButton("Show the prompt…")
        self.prompt_button.clicked.connect(lambda: PromptDialog(self).exec())
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
        buttons.addWidget(self.prompt_button)
        buttons.addWidget(self.forget)
        buttons.addStretch(1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)
        outer.addWidget(self.enabled)
        outer.addWidget(self.unlisted)
        outer.addLayout(form)
        outer.addLayout(status_row)
        outer.addWidget(self.progress)
        outer.addLayout(buttons)
        outer.addWidget(self.note)

    # ------------------------------------------------------------------ settings values
    def load(self, config: AppConfig) -> None:
        smart = config.smart
        self.enabled.setChecked(smart.enabled)
        self.unlisted.setChecked(smart.read_unlisted)
        self.model.setCurrentIndex(max(0, self.model.findData(smart.model)))
        self.gpu.setChecked(smart.use_graphics_card)
        self.learn.setChecked(smart.learn)
        self.per_check.setValue(smart.max_per_check)
        self.keep.setValue(smart.keep_loaded_minutes)
        self.refresh_status()

    def values(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled.isChecked(),
            "read_unlisted": self.unlisted.isChecked(),
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

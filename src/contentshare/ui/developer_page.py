"""The Developer screen: what the program does at each step, and tools to test it.

Shown only when developer mode is on (Settings). Everything here reads; the few buttons that
change something say so on the button."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from contentshare.devtools import tools
from contentshare.devtools.logbuffer import BUFFER
from contentshare.devtools.trace import TRACE
from contentshare.gmail.intake import IntakeReport
from contentshare.selftest import run_checks
from contentshare.ui.context import UiContext
from contentshare.ui.requests_page import summarize_check
from contentshare.ui.widgets import Combo, page_header
from contentshare.ui.widgets import Label as QLabel

LOG_LEVELS = (("Everything", logging.DEBUG), ("Info and up", logging.INFO),
              ("Warnings and errors", logging.WARNING))  # fmt: skip
LOG_REFRESH_MS = 1500


def _output(placeholder: str = "") -> QPlainTextEdit:
    box = QPlainTextEdit()
    box.setReadOnly(True)
    box.setObjectName("audit")  # the monospace look of the History's detail box
    box.setPlaceholderText(placeholder)
    return box


def _row(*widgets: QWidget) -> QHBoxLayout:
    row = QHBoxLayout()
    for widget in widgets:
        row.addWidget(widget)
    return row


class DeveloperPage(QWidget):
    def __init__(self, ctx: UiContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("page")
        self.ctx = ctx
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 26, 16)
        root.setSpacing(14)
        root.addLayout(
            page_header(
                "Developer",
                "What the program does at each step, and tools to test it. "
                "Sign-in tokens are never shown here.",
            )
        )
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self._build_mail_tab()
        self._build_gmail_tab()
        self._build_index_tab()
        self._build_model_tab()
        self._build_logs_tab()
        self._build_database_tab()
        self._build_safety_tab()
        self._timer = QTimer(self)
        self._timer.setInterval(LOG_REFRESH_MS)
        self._timer.timeout.connect(self._tick)
        self.tabs.currentChanged.connect(lambda _i: self.refresh())

    # ------------------------------------------------------------------ helpers
    def _background(
        self, job: Callable[[], Any], show: Callable[[Any], None], title: str, button: QWidget
    ) -> None:
        """Run a slow tool away from the window and show its text (or why it failed)."""
        button.setEnabled(False)

        def done(result: Any) -> None:
            button.setEnabled(True)
            show(result)

        def failed(exc: Exception) -> None:
            button.setEnabled(True)
            show(f"{title} failed: {type(exc).__name__}: {exc}")

        self.ctx.bridge.work(job, done, failed)

    @staticmethod
    def _copy(text: str) -> None:
        QGuiApplication.clipboard().setText(text)

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self.refresh()
        self._timer.start()

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().hideEvent(event)
        self._timer.stop()

    def refresh(self) -> None:
        """Fill the tab that is showing."""
        current = self.tabs.currentWidget()
        if current is self.mail_tab:
            self._show_last_check()
        elif current is self.gmail_tab:
            self.calls.setPlainText(tools.gmail_calls_text())
        elif current is self.index_tab:
            self.index_info.setPlainText(tools.index_summary(self.ctx.controller))
        elif current is self.model_tab:
            self.model_info.setPlainText(tools.model_status(self.ctx.controller))
        elif current is self.logs_tab:
            self._load_switches()
            self._show_logs()
        elif current is self.db_tab:
            self._show_table()
        elif current is self.safety_tab:
            self._load_switches()

    def _tick(self) -> None:
        if self.tabs.currentWidget() is self.logs_tab and self.follow.isChecked():
            self._show_logs()

    # ------------------------------------------------------------------ mail check
    def _build_mail_tab(self) -> None:
        tab = self.mail_tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(10)
        self.check_button = QPushButton("Check now and show the trace")
        self.check_button.setToolTip(
            "Reads new mail exactly like the Check now button on the Requests screen"
        )
        self.check_button.clicked.connect(self._check)
        self.again_button = QPushButton("Read all skipped mail again, then check")
        self.again_button.setToolTip(
            "Forgets that skipped mail was seen (requests already made are not touched), "
            "then checks"
        )
        self.again_button.clicked.connect(self._check_from_scratch)
        copy = QPushButton("Copy the trace")
        copy.clicked.connect(lambda: self._copy(self.trace_text.toPlainText()))
        layout.addLayout(_row(self.check_button, self.again_button, copy))
        self.trace_text: QPlainTextEdit = _output("Click “Check now and show the trace”.")
        layout.addWidget(self.trace_text, 3)

        layout.addWidget(QLabel("Test one email (nothing is read from Gmail or saved)"))
        self.t_sender = QLineEdit(placeholderText="Sender address, for example someone@example.org")
        self.t_subject = QLineEdit(placeholderText="Subject")
        self.t_body = QPlainTextEdit()
        self.t_body.setPlaceholderText("Paste the email text here")
        self.t_body.setMaximumHeight(110)
        layout.addWidget(self.t_sender)
        layout.addWidget(self.t_subject)
        layout.addWidget(self.t_body)
        self.explain_button = QPushButton("Show every decision")
        self.explain_button.clicked.connect(self._explain)
        self.model_button = QPushButton("Also ask the model")
        self.model_button.clicked.connect(self._ask_model)
        layout.addLayout(_row(self.explain_button, self.model_button))
        self.explain_text = _output()
        layout.addWidget(self.explain_text, 2)
        self.tabs.addTab(tab, "Mail check")

    def _show_last_check(self) -> None:
        last = TRACE.last_check()
        if last is not None and self.trace_text.toPlainText() != last.text():
            self.trace_text.setPlainText(last.text())

    def _check(self) -> None:
        self.trace_text.setPlainText("Checking…")

        def show(result: Any) -> None:
            if isinstance(result, IntakeReport):
                self.ctx.say(summarize_check(result))
                self.trace_text.setPlainText("")
                last = TRACE.last_check()
                self.trace_text.setPlainText(
                    last.text() if last else "No trace was kept (is developer mode on?)."
                )
            else:
                self.trace_text.setPlainText(str(result))

        self._background(self.ctx.controller.check_now, show, "The check", self.check_button)

    def _check_from_scratch(self) -> None:
        controller = self.ctx.controller

        def job() -> IntakeReport:
            controller.forget_all_skipped()
            return controller.check_now()

        self.trace_text.setPlainText("Reading everything again…")

        def show(result: Any) -> None:
            last = TRACE.last_check()
            self.trace_text.setPlainText(
                last.text() if isinstance(result, IntakeReport) and last else str(result)
            )

        self._background(job, show, "The check", self.again_button)

    def _explain(self) -> None:
        controller = self.ctx.controller
        sender, subject, body = (
            self.t_sender.text(),
            self.t_subject.text(),
            self.t_body.toPlainText(),
        )
        self._background(
            lambda: tools.explain_email(controller, sender, subject, body),
            self.explain_text.setPlainText,
            "The explanation",
            self.explain_button,
        )

    def _ask_model(self) -> None:
        controller = self.ctx.controller
        subject, body = self.t_subject.text(), self.t_body.toPlainText()
        self.explain_text.appendPlainText("\nAsking the model…")

        def show(text: Any) -> None:
            self.explain_text.appendPlainText("\n== Local model ==\n" + str(text))

        self._background(
            lambda: tools.try_model(controller, subject, body), show, "The model", self.model_button
        )

    # ------------------------------------------------------------------ Gmail
    def _build_gmail_tab(self) -> None:
        tab = self.gmail_tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(10)
        self.account_button = QPushButton("Show the account")
        self.account_button.clicked.connect(self._account)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        clear = QPushButton("Clear")
        clear.clicked.connect(self._clear_calls)
        layout.addLayout(_row(self.account_button, refresh, clear))
        self.account_text = _output()
        self.account_text.setMaximumHeight(110)
        layout.addWidget(self.account_text)
        layout.addWidget(QLabel("Gmail calls (newest last): which call, how long, what came back"))
        self.calls = _output()
        layout.addWidget(self.calls, 3)
        layout.addWidget(QLabel("Run a Gmail search and see the raw results"))
        self.query = QLineEdit(text="in:inbox newer_than:7d")
        self.query_button = QPushButton("Run this search")
        self.query_button.clicked.connect(self._run_query)
        layout.addLayout(_row(self.query, self.query_button))
        self.query_text = _output()
        layout.addWidget(self.query_text, 3)
        self.tabs.addTab(tab, "Gmail")

    def _account(self) -> None:
        controller = self.ctx.controller
        self._background(
            lambda: tools.account_info(controller),
            self.account_text.setPlainText,
            "The account",
            self.account_button,
        )

    def _clear_calls(self) -> None:
        TRACE.clear()
        self.refresh()

    def _run_query(self) -> None:
        controller, query = self.ctx.controller, self.query.text().strip()
        if not query:
            return
        self.query_text.setPlainText("Searching…")
        self._background(
            lambda: tools.run_gmail_query(controller, query),
            self.query_text.setPlainText,
            "The search",
            self.query_button,
        )

    # ------------------------------------------------------------------ index
    def _build_index_tab(self) -> None:
        tab = self.index_tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(10)
        self.index_info = _output()
        self.index_info.setMaximumHeight(150)
        layout.addWidget(self.index_info)
        layout.addWidget(QLabel("Search the index: how a name scores, and why"))
        self.index_query = QLineEdit(placeholderText="A program or folder name")
        self.index_button = QPushButton("Search")
        self.index_button.clicked.connect(self._search_index)
        self.index_query.returnPressed.connect(self._search_index)
        layout.addLayout(_row(self.index_query, self.index_button))
        self.index_result = _output()
        layout.addWidget(self.index_result, 1)
        self.tabs.addTab(tab, "Index")

    def _search_index(self) -> None:
        controller, text = self.ctx.controller, self.index_query.text()
        if not text.strip():
            return
        self._background(
            lambda: tools.index_search(controller, text),
            self.index_result.setPlainText,
            "The search",
            self.index_button,
        )

    # ------------------------------------------------------------------ model
    def _build_model_tab(self) -> None:
        tab = self.model_tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(10)
        self.model_info = _output()
        self.model_info.setMaximumHeight(170)
        layout.addWidget(self.model_info)
        layout.addWidget(QLabel("Try the model on a text (the answer, its time and prompt size)"))
        self.model_subject = QLineEdit(placeholderText="Subject")
        self.model_body = QPlainTextEdit()
        self.model_body.setPlaceholderText("Email text")
        self.model_body.setMaximumHeight(130)
        layout.addWidget(self.model_subject)
        layout.addWidget(self.model_body)
        self.model_try = QPushButton("Ask the model")
        self.model_try.clicked.connect(self._model_try)
        layout.addWidget(self.model_try)
        self.model_result = _output()
        layout.addWidget(self.model_result, 1)
        self.tabs.addTab(tab, "Model")

    def _model_try(self) -> None:
        controller = self.ctx.controller
        subject, body = self.model_subject.text(), self.model_body.toPlainText()
        self.model_result.setPlainText("Asking the model…")
        self._background(
            lambda: tools.try_model(controller, subject, body),
            self.model_result.setPlainText,
            "The model",
            self.model_try,
        )

    # ------------------------------------------------------------------ logs
    def _build_logs_tab(self) -> None:
        tab = self.logs_tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(10)
        self.level = Combo()
        for name, level in LOG_LEVELS:
            self.level.addItem(name, level)
        self.level.currentIndexChanged.connect(lambda _i: self._show_logs())
        self.filter = QLineEdit(placeholderText="Search the log")
        self.filter.textChanged.connect(lambda _t: self._show_logs())
        self.follow = QCheckBox("Follow")
        self.follow.setChecked(True)
        self.debug = QCheckBox("Keep every detail (debug)")
        self.debug.toggled.connect(self._toggle_debug)
        layout.addLayout(_row(self.level, self.filter, self.follow, self.debug))
        self.log_text = _output("No log lines yet.")
        layout.addWidget(self.log_text, 1)
        self.include_text = QCheckBox("Include subjects and senders in the copied report")
        self.include_text.toggled.connect(
            lambda on: self.ctx.controller.set_dev_option("include_mail_text", on)
        )
        copy = QPushButton("Copy diagnostics")
        copy.setToolTip("Versions, settings without secrets, the last check, calls and log lines")
        copy.clicked.connect(self._copy_diagnostics)
        clear = QPushButton("Clear the log")
        clear.clicked.connect(self._clear_log)
        layout.addLayout(_row(self.include_text, copy, clear))
        self.tabs.addTab(tab, "Log")

    def _show_logs(self) -> None:
        lines = BUFFER.lines(int(self.level.currentData() or 0), self.filter.text())
        text = "\n".join(lines[-1500:])
        if text != self.log_text.toPlainText():
            self.log_text.setPlainText(text)
            if self.follow.isChecked():
                bar = self.log_text.verticalScrollBar()
                bar.setValue(bar.maximum())

    def _clear_log(self) -> None:
        BUFFER.clear()
        self._show_logs()

    def _toggle_debug(self, on: bool) -> None:
        if self.ctx.controller.config.dev.debug_log != on:
            self.ctx.controller.set_dev_option("debug_log", on)

    def _copy_diagnostics(self) -> None:
        controller = self.ctx.controller
        text = tools.diagnostics(
            controller, include_mail_text=controller.config.dev.include_mail_text
        )
        self._copy(text)
        self.ctx.say("The diagnostics were copied. Paste them where you need them.")

    # ------------------------------------------------------------------ database
    def _build_database_tab(self) -> None:
        tab = self.db_tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(10)
        self.table_choice = Combo()
        for name in tools.TABLES:
            self.table_choice.addItem(name, name)
        self.table_choice.currentIndexChanged.connect(lambda _i: self._show_table())
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._show_table)
        note = QLabel("Read only. The newest 100 rows. Password and token columns are hidden.")
        note.setProperty("role", "muted")
        layout.addLayout(_row(self.table_choice, refresh))
        layout.addWidget(note)
        self.db_table = QTableWidget(0, 0)
        self.db_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.db_table.setAccessibleName("Database rows")
        self.db_table.verticalHeader().setVisible(False)
        layout.addWidget(self.db_table, 1)
        self.tabs.addTab(tab, "Database")

    def _show_table(self) -> None:
        name = str(self.table_choice.currentData() or tools.TABLES[0])
        try:
            columns, rows = tools.table_rows(self.ctx.controller, name)
        except Exception as exc:
            columns, rows = ["error"], [[f"{type(exc).__name__}"]]
        table = self.db_table
        table.setColumnCount(len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                table.setItem(r, c, QTableWidgetItem(value))
        table.resizeColumnsToContents()

    # ------------------------------------------------------------------ safety and testing
    def _build_safety_tab(self) -> None:
        tab = self.safety_tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(10)
        self.no_replies = QCheckBox("Do not send replies (reading still works)")
        self.fake_gmail = QCheckBox(
            "Use test emails instead of the real mailbox (nothing real is read or sent)"
        )
        self.dry = QCheckBox("Dry run: approving copies nothing")
        for box, name in ((self.no_replies, "no_replies"), (self.fake_gmail, "fake_gmail")):
            box.toggled.connect(lambda on, n=name: self._switch(n, on))
        self.dry.toggled.connect(self._switch_dry)
        for box in (self.no_replies, self.fake_gmail, self.dry):
            layout.addWidget(box)
        hint = QLabel(
            "With the test mailbox on, “Check now” finds five canned emails: one from a "
            "department sender, two strangers asking for content, a newsletter and a greeting."
        )
        hint.setWordWrap(True)
        hint.setProperty("role", "muted")
        layout.addWidget(hint)
        self.selftest_button = QPushButton("Run the self-test")
        self.selftest_button.clicked.connect(self._selftest)
        layout.addWidget(self.selftest_button)
        self.selftest_text = _output()
        layout.addWidget(self.selftest_text, 1)
        self.tabs.addTab(tab, "Safety and tests")

    def _load_switches(self) -> None:
        dev = self.ctx.controller.config.dev
        for box, value in (
            (self.no_replies, dev.no_replies),
            (self.fake_gmail, dev.fake_gmail),
            (self.dry, self.ctx.controller.dry_run),
            (self.debug, dev.debug_log),
            (self.include_text, dev.include_mail_text),
        ):
            box.blockSignals(True)
            box.setChecked(value)
            box.blockSignals(False)

    def _switch(self, name: str, on: bool) -> None:
        self.ctx.controller.set_dev_option(name, on)

    def _switch_dry(self, on: bool) -> None:
        try:
            self.ctx.controller.set_dry_run(on)
        except Exception as exc:
            self.ctx.fail(self, "Could not change dry run", exc)
            self._load_switches()

    def _selftest(self) -> None:
        def job() -> str:
            return "\n".join(
                f"{'ok  ' if ok else 'FAIL'} {name}: {detail}" for name, ok, detail in run_checks()
            )

        self.selftest_text.setPlainText("Running…")
        self._background(
            job, self.selftest_text.setPlainText, "The self-test", self.selftest_button
        )

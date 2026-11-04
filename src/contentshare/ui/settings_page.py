"""Settings: folders, departments, Gmail, matching and copy options."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError
from PySide6.QtCore import Qt, QTime, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

import contentshare
from contentshare.config.models import AppConfig, Department
from contentshare.config.templates import REPLY_PLACEHOLDERS, SUBFOLDER_PLACEHOLDERS
from contentshare.ui.context import UiContext
from contentshare.ui.folder_list import FolderList
from contentshare.ui.pickers import choose_file, choose_folder
from contentshare.ui.timefmt import local_stamp
from contentshare.ui.widgets import Label as QLabel
from contentshare.ui.widgets import SafeMessageBox as QMessageBox
from contentshare.ui.widgets import page_header

log = logging.getLogger(__name__)

HASHES = ["xxh3_128", "blake3", "sha256"]
APPEARANCES = [("system", "Same as Windows"), ("light", "Light"), ("dark", "Dark")]


def lines(text: str) -> list[str]:
    """One entry per line or comma, blanks dropped."""
    return [p.strip() for chunk in text.splitlines() for p in chunk.split(",") if p.strip()]


def explain_validation(exc: ValidationError) -> str:
    out = []
    for err in exc.errors():
        where = " → ".join(str(p) for p in err["loc"])
        out.append(f"• {where}: {err['msg'].removeprefix('Value error, ')}")
    return "\n".join(out)


class DepartmentDialog(QDialog):
    def __init__(self, department: Department | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Department")
        self.resize(560, 520)
        d = department
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(d.name if d else "")
        self.emails = QPlainTextEdit("\n".join(d.sender_emails) if d else "")
        self.emails.setFixedHeight(70)
        self.emails.setPlaceholderText("anita@example.org (one per line)")
        self.domains = QPlainTextEdit("\n".join(d.sender_domains) if d else "")
        self.domains.setFixedHeight(50)
        self.domains.setPlaceholderText("example.org (anyone at this domain; optional)")
        self.root = QLineEdit(d.destination_root if d else "")
        self.root.setPlaceholderText(r"\\nas02\media\Incoming   or   D:\Incoming")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        root_row = QHBoxLayout()
        root_row.addWidget(self.root, 1)
        root_row.addWidget(browse)
        self.pattern = QLineEdit(d.subfolder_pattern if d else "{program}")
        self.reply = QPlainTextEdit(d.reply_template or "" if d else "")
        self.reply.setPlaceholderText("Leave empty to use the default reply text")
        form.addRow("Name", self.name)
        form.addRow("Sender emails", self.emails)
        form.addRow("Sender domains", self.domains)
        form.addRow("Copy into", root_row)
        form.addRow("Subfolder", self.pattern)
        form.addRow("Reply text", self.reply)
        layout.addLayout(form)
        hint = QLabel(
            "Subfolder can use: " + ", ".join(f"{{{p}}}" for p in sorted(SUBFOLDER_PLACEHOLDERS))
            + ".  Reply text can use: " + ", ".join(f"{{{p}}}" for p in sorted(REPLY_PLACEHOLDERS))
        )  # fmt: skip
        hint.setWordWrap(True)
        hint.setProperty("role", "muted")
        layout.addWidget(hint)
        box = QDialogButtonBox()
        ok = box.addButton("Save department", QDialogButtonBox.ButtonRole.AcceptRole)
        ok.setProperty("primary", True)
        box.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)
        self.result_department: Department | None = None

    def _browse(self) -> None:
        chosen = choose_folder(self, "Choose the destination folder")
        if chosen:
            self.root.setText(chosen)

    def _accept(self) -> None:
        try:
            self.result_department = Department(
                name=self.name.text(),
                sender_emails=lines(self.emails.toPlainText()),
                sender_domains=lines(self.domains.toPlainText()),
                destination_root=self.root.text().strip(),
                subfolder_pattern=self.pattern.text().strip() or "{program}",
                reply_template=self.reply.toPlainText().strip() or None,
            )
        except ValidationError as exc:
            QMessageBox.warning(self, "Check the department", explain_validation(exc))
            return
        self.accept()


class SettingsPage(QWidget):
    saved = Signal()  # settings were stored (the window re-reads schedule and tray options)
    open_help = Signal(str)  # a Help topic to show, for example "gmail"
    update_check_requested = Signal()  # "Check for updates now"
    appearance_changed = Signal(str)  # "system" | "light" | "dark": show it now (not yet saved)

    def __init__(self, ctx: UiContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("page")
        self.ctx = ctx
        self.departments: list[Department] = []
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 26, 16)
        root.setSpacing(14)
        root.addLayout(
            page_header("Settings", "Folders, departments, Gmail and how copies are made.")
        )
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self._build_general()
        self._build_departments()
        self._build_gmail()
        self._build_matching()
        self._build_index()
        bar = QHBoxLayout()
        self.message = QLabel()
        self.message.setProperty("role", "muted")
        bar.addWidget(self.message, 1)
        revert = QPushButton("Revert")
        revert.clicked.connect(self.load)
        self.save_button = QPushButton("Save settings")
        self.save_button.setProperty("primary", True)
        self.save_button.clicked.connect(self.save)
        bar.addWidget(revert)
        bar.addWidget(self.save_button)
        root.addLayout(bar)
        self.load()

    # ------------------------------------------------------------------ tabs
    def _tab(self, name: str) -> QFormLayout:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(4, 20, 14, 24)
        form.setHorizontalSpacing(32)
        form.setVerticalSpacing(14)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(page)
        self.tabs.addTab(scroll, name)
        return form

    @staticmethod
    def _section(form: QFormLayout, title: str, first: bool = False) -> None:
        """A small heading that starts a group of rows."""
        label = QLabel(title.upper())
        label.setProperty("role", "section")
        label.setContentsMargins(0, 0 if first else 12, 0, 0)
        form.addRow(label)

    def _build_general(self) -> None:
        form = self._tab("General")
        self._section(form, "Appearance", first=True)
        self.appearance = QComboBox()
        self.appearance.setAccessibleName("Appearance")
        self.appearance.setMaximumWidth(260)
        for value, text in APPEARANCES:
            self.appearance.addItem(text, value)
        self.appearance.activated.connect(
            lambda _i: self.appearance_changed.emit(self._appearance())
        )
        form.addRow("Theme", self.appearance)

        self._section(form, "Daily check")
        self.daily_enabled = QCheckBox("Check for requests every day")
        self.check_time = QTimeEdit()
        self.check_time.setDisplayFormat("HH:mm")
        self.check_time.setAccessibleName("Daily check time")
        self.check_time.setMaximumWidth(140)
        self.missed = QCheckBox(
            "Check for requests when the app starts if the daily check was missed"
        )
        form.addRow("", self.daily_enabled)
        form.addRow("Daily check at", self.check_time)
        form.addRow("", self.missed)

        self._section(form, "Windows")
        self.start_login = QCheckBox("Start ContentShare when I sign in to Windows")
        self.start_note = QLabel()
        self.start_note.setWordWrap(True)
        self.start_note.setProperty("role", "small")
        self.to_tray = QCheckBox("Keep running in the tray when the window is closed")
        self.notify = QCheckBox("Show pop-up notifications (counts only, never email text)")
        startup = QWidget()
        startup_box = QVBoxLayout(startup)
        startup_box.setContentsMargins(0, 0, 0, 0)
        startup_box.setSpacing(4)
        startup_box.addWidget(self.start_login)
        startup_box.addWidget(self.start_note)
        form.addRow("", startup)
        form.addRow("", self.to_tray)
        form.addRow("", self.notify)

        self._section(form, "Safety")
        self.auto_default = QCheckBox("Tick “send the reply automatically” on new requests")
        self.dry_run = QCheckBox(
            "Dry-run mode: check everything, but copy nothing and send nothing"
        )
        form.addRow("", self.auto_default)
        form.addRow("", self.dry_run)
        self._build_updates_and_content(form)

    def _build_updates_and_content(self, form: QFormLayout) -> None:
        self._section(form, "Updates")
        self.check_updates = QCheckBox("Check for updates when ContentShare starts")
        self.update_now = QPushButton("Check for updates now")
        self.update_now.clicked.connect(self.update_check_requested.emit)
        version = QLabel(f"You have ContentShare {contentshare.__version__}")
        version.setProperty("role", "small")
        updates_row = QHBoxLayout()
        updates_row.addWidget(self.update_now)
        updates_row.addWidget(version, 1)
        form.addRow("", self.check_updates)
        form.addRow("", updates_row)

        self._section(form, "Content")
        self.folders = FolderList(dialog_title="Choose a storage folder")
        self.roots = self.folders.list  # the list itself, for callers that read it directly
        self.folders.setMinimumHeight(150)
        form.addRow("Storage folders to search", self.folders)

        self.default_reply = QPlainTextEdit()
        self.default_reply.setFixedHeight(150)
        self.default_reply.setAccessibleName("Default reply text")
        form.addRow("Default reply text", self.default_reply)
        hint = QLabel("Placeholders: " + ", ".join(f"{{{p}}}" for p in sorted(REPLY_PLACEHOLDERS)))
        hint.setProperty("role", "small")
        form.addRow("", hint)

    def _build_departments(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(
            QLabel("Only emails from these senders are read. Each department has one folder.")
        )
        self.dept_table = QTableWidget(0, 3)
        self.dept_table.setHorizontalHeaderLabels(["Department", "Senders", "Copies go to"])
        self.dept_table.setAccessibleName("Departments")
        self.dept_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.dept_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.dept_table.verticalHeader().setVisible(False)
        self.dept_table.verticalHeader().setDefaultSectionSize(42)
        self.dept_table.setShowGrid(False)
        header = self.dept_table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setHighlightSections(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.dept_table.doubleClicked.connect(lambda _i: self._edit_department())
        layout.addWidget(self.dept_table, 1)
        row = QHBoxLayout()
        for text, slot in (
            ("Add…", self._add_department),
            ("Edit…", self._edit_department),
            ("Remove", self._remove_department),
        ):
            button = QPushButton(text)
            button.clicked.connect(lambda _c=False, s=slot: s())
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)
        self.tabs.addTab(page, "Departments")

    def _build_gmail(self) -> None:
        form = self._tab("Gmail")
        self.secret = QLineEdit()
        self.secret.setAccessibleName("Client secret file")
        self.secret.setPlaceholderText("client_secret.json downloaded from Google Cloud")
        pick = QPushButton("Browse…")
        pick.clicked.connect(self._browse_secret)
        row = QHBoxLayout()
        row.addWidget(self.secret, 1)
        row.addWidget(pick)
        form.addRow("Client file", row)
        how = QPushButton("How do I get this file?")
        how.clicked.connect(lambda: self.open_help.emit("gmail"))
        form.addRow("", how)
        self.gmail_state = QLabel()
        self.connect_button = QPushButton("Connect Gmail…")
        self.connect_button.clicked.connect(self._connect)
        self.disconnect_button = QPushButton("Disconnect")
        self.disconnect_button.clicked.connect(self._disconnect)
        buttons = QHBoxLayout()
        buttons.addWidget(self.gmail_state, 1)
        buttons.addWidget(self.connect_button)
        buttons.addWidget(self.disconnect_button)
        form.addRow("Status", buttons)
        self.label = QCheckBox(
            "Label processed emails “ContentShare/Processed” (needs gmail.modify)"
        )
        self.require_auth = QCheckBox("Treat a sender as verified only if Gmail confirms it")
        self.lookback = QSpinBox()
        self.lookback.setRange(1, 365)
        self.lookback.setMaximumWidth(160)
        self.lookback.setSuffix(" days")
        self.max_messages = QSpinBox()
        self.max_messages.setRange(1, 500)
        self.max_messages.setMaximumWidth(160)
        form.addRow("", self.label)
        form.addRow("", self.require_auth)
        form.addRow("Look back", self.lookback)
        form.addRow("Emails per check", self.max_messages)
        note = QLabel(
            "Changing the label option changes the permissions Gmail is asked for: "
            "disconnect and connect again afterwards."
        )
        note.setWordWrap(True)
        note.setProperty("role", "muted")
        form.addRow("", note)

    def _build_matching(self) -> None:
        form = self._tab("Matching and copying")
        self.ready_score = QSpinBox()
        self.ready_score.setRange(50, 100)
        self.ready_score.setMaximumWidth(160)
        self.gap = QSpinBox()
        self.gap.setRange(0, 50)
        self.gap.setMaximumWidth(160)
        self.keep_checksums = QCheckBox("Keep a checksum file in each copied folder")
        self.hash = QComboBox()
        self.hash.addItems(HASHES)
        self.hash.setMaximumWidth(220)
        self.concurrency = QSpinBox()
        self.concurrency.setRange(1, 4)
        self.concurrency.setMaximumWidth(160)
        self.attempts = QSpinBox()
        self.attempts.setRange(1, 10)
        self.attempts.setMaximumWidth(160)
        form.addRow("Mark Ready when at least", self.ready_score)
        form.addRow("…and clearly ahead of the next by", self.gap)
        form.addRow("Verification checksum", self.hash)
        form.addRow("", self.keep_checksums)
        form.addRow("Copies at the same time", self.concurrency)
        form.addRow("Attempts per file", self.attempts)
        note = QLabel(
            "Lower the Ready score only after checking real matches in the review list: "
            "a wrong automatic match is worse than a request that waits for you."
        )
        note.setWordWrap(True)
        note.setProperty("role", "muted")
        form.addRow("", note)

    def _build_index(self) -> None:
        form = self._tab("Index")
        self.index_state = QLabel()
        self.index_state.setWordWrap(True)
        self.scan_button = QPushButton("Refresh index now")
        self.scan_button.clicked.connect(self._scan)
        form.addRow("Storage index", self.index_state)
        form.addRow("", self.scan_button)
        note = QLabel(
            "The index lists folder and file names on the storage server so requests can be "
            "matched quickly. It only reads. A large server can take a while."
        )
        note.setWordWrap(True)
        note.setProperty("role", "muted")
        form.addRow("", note)

    # ------------------------------------------------------------------ load / save
    def load(self) -> None:
        c = self.ctx.controller.config
        hour, minute = (int(p) for p in c.schedule.daily_check_time.split(":"))
        self.check_time.setTime(QTime(hour, minute))
        self.appearance.setCurrentIndex(self.appearance.findData(c.appearance))
        self.appearance_changed.emit(c.appearance)  # also undoes an unsaved preview (Revert)
        self.missed.setChecked(c.schedule.run_missed_check_on_startup)
        self.daily_enabled.setChecked(c.schedule.daily_check_enabled)
        self.to_tray.setChecked(c.schedule.close_to_tray)
        self.notify.setChecked(c.schedule.notifications)
        self.start_login.setChecked(c.schedule.start_with_windows)
        self._show_startup_note()
        self.auto_default.setChecked(c.auto_reply_default)
        self.dry_run.setChecked(c.dry_run)
        self.check_updates.setChecked(c.updates.check_on_start)
        self.folders.set_paths(c.source_roots)
        self.default_reply.setPlainText(c.default_reply_template)
        self.departments = [d.model_copy() for d in c.departments]
        self._fill_departments()
        self.secret.setText(c.gmail.client_secret_path or "")
        self.label.setChecked(c.gmail.apply_label)
        self.require_auth.setChecked(c.gmail.require_authentication)
        self.lookback.setValue(c.gmail.lookback_days)
        self.max_messages.setValue(c.gmail.max_messages)
        self.ready_score.setValue(c.matching.auto_ready_score)
        self.gap.setValue(c.matching.ambiguity_gap)
        self.hash.setCurrentText(c.copy_options.hash_algorithm)
        self.keep_checksums.setChecked(c.copy_options.write_checksum_file)
        self.concurrency.setValue(c.copy_options.concurrency)
        self.attempts.setValue(c.copy_options.max_attempts)
        self._refresh_status()
        self.message.setText("")

    def collect(self) -> AppConfig:
        """The settings as typed. Raises ``ValidationError`` with a readable cause."""
        base: dict[str, Any] = self.ctx.controller.config.model_dump()
        base["schedule"].update(
            daily_check_time=self.check_time.time().toString("HH:mm"),
            run_missed_check_on_startup=self.missed.isChecked(),
            daily_check_enabled=self.daily_enabled.isChecked(),
            close_to_tray=self.to_tray.isChecked(),
            notifications=self.notify.isChecked(),
            start_with_windows=self.start_login.isChecked(),
        )
        base["appearance"] = self._appearance()
        base["auto_reply_default"] = self.auto_default.isChecked()
        base["dry_run"] = self.dry_run.isChecked()
        base["updates"]["check_on_start"] = self.check_updates.isChecked()
        base["source_roots"] = self.folders.paths()
        typed = self.default_reply.toPlainText()
        if not typed.strip():
            raise ValueError("The default reply text must not be empty")
        base["default_reply_template"] = typed
        base["departments"] = [d.model_dump() for d in self.departments]
        base["gmail"].update(
            client_secret_path=self.secret.text().strip() or None,
            apply_label=self.label.isChecked(),
            require_authentication=self.require_auth.isChecked(),
            lookback_days=self.lookback.value(),
            max_messages=self.max_messages.value(),
        )
        base["matching"].update(
            auto_ready_score=self.ready_score.value(), ambiguity_gap=self.gap.value()
        )
        base["copy_options"].update(
            hash_algorithm=self.hash.currentText(),
            write_checksum_file=self.keep_checksums.isChecked(),
            concurrency=self.concurrency.value(),
            max_attempts=self.attempts.value(),
        )
        return AppConfig.model_validate(base)

    def save(self) -> bool:
        try:
            config = self.collect()
        except ValidationError as exc:
            QMessageBox.warning(self, "Check the settings", explain_validation(exc))
            return False
        except ValueError as exc:
            QMessageBox.warning(self, "Check the settings", str(exc))
            return False
        try:
            self.ctx.controller.apply_config(config)
        except Exception as exc:
            self.ctx.fail(self, "Settings not saved", exc)
            return False
        problem = self._apply_startup(config.schedule.start_with_windows)
        self.message.setText(f"Settings saved. {problem}".strip())
        self.ctx.say("Settings saved")
        self._refresh_status()
        self._show_startup_note()
        self.saved.emit()
        return True

    def _appearance(self) -> str:
        return str(self.appearance.currentData() or "system")

    # ------------------------------------------------------------------ start at login
    def _show_startup_note(self) -> None:
        text = self._startup_text()
        self.start_note.setText(text)
        self.start_note.setVisible(bool(text))

    def _startup_text(self) -> str:
        startup = self.ctx.startup
        if startup is None:
            return ""
        try:
            status = startup.status()
        except OSError as exc:
            return f"Windows start-up could not be read ({exc})."
        if not status.enabled:
            return "Not set to start with Windows."
        how = "Windows start-up list" if status.method == "registry" else "Task Scheduler"
        stale = "" if status.current else " It points to an old location and is fixed on save."
        return f"Starts with Windows through the {how}, minimised in the tray.{stale}"

    def _apply_startup(self, wanted: bool) -> str:
        """Make Windows match the setting. Returns a sentence only when something went wrong."""
        if self.ctx.startup is None:
            return ""
        try:
            self.ctx.startup.apply(wanted)
        except OSError as exc:
            log.warning("start with Windows could not be changed: %s", exc)
            return (
                "Windows would not let ContentShare change its start-up entry. "
                "You can add it yourself in Settings → Apps → Startup."
            )
        return ""

    # ------------------------------------------------------------------ helpers
    def _fill_departments(self) -> None:
        self.dept_table.setRowCount(len(self.departments))
        for row, d in enumerate(self.departments):
            senders = ", ".join([*d.sender_emails, *(f"@{x}" for x in d.sender_domains)])
            for col, text in enumerate([d.name, senders, d.destination_root]):
                self.dept_table.setItem(row, col, QTableWidgetItem(text))

    def _add_department(self) -> None:
        dialog = DepartmentDialog(None, self)
        if dialog.exec() and dialog.result_department:
            self.departments.append(dialog.result_department)
            self._fill_departments()

    def _edit_department(self) -> None:
        row = self.dept_table.currentRow()
        if row < 0:
            return
        dialog = DepartmentDialog(self.departments[row], self)
        if dialog.exec() and dialog.result_department:
            self.departments[row] = dialog.result_department
            self._fill_departments()

    def _remove_department(self) -> None:
        row = self.dept_table.currentRow()
        if row >= 0:
            del self.departments[row]
            self._fill_departments()

    def _browse_secret(self) -> None:
        path = choose_file(self, "Choose client_secret.json", "JSON (*.json)")
        if path:
            self.secret.setText(path)

    def _refresh_status(self) -> None:
        controller = self.ctx.controller
        connected = controller.gmail_connected()
        self.gmail_state.setText("✔ Connected" if connected else "Not connected")
        self.connect_button.setEnabled(not connected)
        self.disconnect_button.setEnabled(connected)
        summary = controller.index_summary()
        when = local_stamp(summary["last_scan"], empty="never")
        self.index_state.setText(
            f"{summary['items']:,} items indexed ({summary['folders']:,} folders); "
            f"last complete scan: {when}"
        )

    def _connect(self) -> None:
        if not self.save():
            return
        self.connect_button.setEnabled(False)
        self.connect_button.setText("Waiting for the browser…")

        def done(_r: object) -> None:
            self.connect_button.setText("Connect Gmail…")
            self._refresh_status()
            self.ctx.say("Gmail connected")

        def failed(exc: Exception) -> None:
            self.connect_button.setText("Connect Gmail…")
            self._refresh_status()
            self.ctx.fail(self, "Gmail was not connected", exc)

        self.ctx.bridge.work(self.ctx.controller.connect_gmail, done, failed)

    def _disconnect(self) -> None:
        answer = QMessageBox.question(
            self, "Disconnect Gmail",
            "Revoke ContentShare's access to Gmail and delete the saved sign-in?",
        )  # fmt: skip
        if answer != QMessageBox.StandardButton.Yes:
            return

        def done(text: str) -> None:
            self._refresh_status()
            QMessageBox.information(self, "Disconnected", text)

        self.ctx.bridge.work(
            self.ctx.controller.disconnect_gmail,
            done,
            lambda e: self.ctx.fail(self, "Disconnect", e),
        )

    def _scan(self) -> None:
        if not self.ctx.controller.config.source_roots:
            QMessageBox.information(
                self, "Index", "Add a storage folder in General and save first."
            )
            return
        self.scan_button.setEnabled(False)
        self.index_state.setText("Scanning… this can take a few minutes on a large server.")

        def done(reports: list[Any]) -> None:
            self.scan_button.setEnabled(True)
            self._refresh_status()
            bad = [r for r in reports if r.status != "complete"]
            if bad:
                QMessageBox.warning(
                    self, "Index",
                    "\n".join(f"{r.root}: {r.error or r.status}" for r in bad)
                    + "\n\nThe previous index was kept.",
                )  # fmt: skip

        def failed(exc: Exception) -> None:
            self.scan_button.setEnabled(True)
            self._refresh_status()
            self.ctx.fail(self, "Index", exc)

        self.ctx.bridge.work(self.ctx.controller.rescan_index, done, failed)

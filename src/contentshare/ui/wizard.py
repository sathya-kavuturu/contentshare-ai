"""First-run setup: where the content lives, who may ask for it, and where it goes."""

from __future__ import annotations

from pydantic import ValidationError
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from contentshare.config.models import AppConfig, Department
from contentshare.ui.folder_list import FolderList
from contentshare.ui.settings_page import explain_validation, lines
from contentshare.ui.widgets import Label as QLabel
from contentshare.ui.widgets import SafeMessageBox as QMessageBox


def _note(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    return label


class FirstRunWizard(QWizard):
    """Collects a minimal, valid configuration. Everything can be changed later in Settings."""

    def __init__(self, base: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Welcome to ContentShare")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.resize(680, 520)
        self._base = base
        self.result_config: AppConfig | None = None

        self.addPage(self._welcome())
        self.storage_page = self._storage()
        self.addPage(self.storage_page)
        self.department_page = self._department()
        self.addPage(self.department_page)
        self.addPage(self._finish())

    # ------------------------------------------------------------------ pages
    def _welcome(self) -> QWizardPage:
        page = QWizardPage()
        page.setTitle("What ContentShare does")
        layout = QVBoxLayout(page)
        layout.addWidget(
            _note(
                "ContentShare reads request emails from the departments you list, finds the "
                "program content on your storage server, copies it to the department's folder "
                "with every file checked against the original, and replies in the same email "
                "conversation.\n\n"
                "Nothing is copied and no reply is sent until you click Approve. Two quick "
                "questions now; everything can be changed later in Settings."
            )
        )
        return page

    def _storage(self) -> QWizardPage:
        page = QWizardPage()
        page.setTitle("Where is the program content?")
        layout = QVBoxLayout(page)
        layout.addWidget(
            _note(
                "Add the folder (or folders) on the storage server that hold the programs. "
                r"Network paths such as \\storage01\Programs and drive letters both work. "
                "ContentShare only reads here; it never changes or deletes anything."
            )
        )
        self.folders = FolderList(dialog_title="Choose a storage folder")
        self.roots = self.folders.list  # the list itself, for callers that read it directly
        self.typed = self.folders.typed
        layout.addWidget(self.folders, 1)
        layout.addWidget(
            _note("Added the wrong one? Select it, then Edit (or double-click) it, or Remove.")
        )
        return page

    def _department(self) -> QWizardPage:
        page = QWizardPage()
        page.setTitle("Your first department")
        form = QFormLayout(page)
        form.addRow(
            _note(
                "Only emails from these senders are ever read. Add more departments later "
                "in Settings."
            )
        )
        self.dept_name = QLineEdit()
        self.dept_name.setAccessibleName("Department name")
        self.dept_emails = QPlainTextEdit()
        self.dept_emails.setFixedHeight(70)
        self.dept_emails.setPlaceholderText("anita@example.org (one per line)")
        self.dept_domains = QPlainTextEdit()
        self.dept_domains.setFixedHeight(46)
        self.dept_domains.setPlaceholderText("or a whole domain: example.org")
        self.dept_root = QLineEdit()
        self.dept_root.setPlaceholderText(r"\\nas02\media\Incoming")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_dest)
        root_row = QHBoxLayout()
        root_row.addWidget(self.dept_root, 1)
        root_row.addWidget(browse)
        form.addRow("Name", self.dept_name)
        form.addRow("Sender emails", self.dept_emails)
        form.addRow("Sender domains", self.dept_domains)
        form.addRow("Copy into", root_row)
        return page

    def _finish(self) -> QWizardPage:
        page = QWizardPage()
        page.setTitle("Ready")
        layout = QVBoxLayout(page)
        layout.addWidget(
            _note(
                "Next: Settings → Gmail to connect your mailbox (Help → Connect Gmail explains "
                "the Google steps one by one), then Settings → Index to read the storage "
                "server. The Help screen also explains every setting."
            )
        )
        self.dry = QCheckBox("Start in dry-run mode (recommended): check everything, copy nothing")
        self.dry.setChecked(True)
        layout.addWidget(self.dry)
        layout.addWidget(
            _note(
                "In dry-run mode Approve only reports what would be copied and no email is "
                "sent. Switch it off in Settings when you are happy with the results."
            )
        )
        return page

    # ------------------------------------------------------------------ helpers
    def _add_typed(self) -> None:
        self.folders.add_typed()

    def _browse_dest(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Choose the destination folder")
        if chosen:
            self.dept_root.setText(chosen.replace("/", "\\"))

    def build_config(self) -> AppConfig:
        """Raises ``ValidationError`` when something typed here is not acceptable."""
        roots = self.folders.paths()
        department = Department(
            name=self.dept_name.text(),
            sender_emails=lines(self.dept_emails.toPlainText()),
            sender_domains=lines(self.dept_domains.toPlainText()),
            destination_root=self.dept_root.text().strip(),
        )
        data = self._base.model_dump()
        data.update(
            source_roots=roots,
            departments=[department.model_dump()],
            dry_run=self.dry.isChecked(),
        )
        return AppConfig.model_validate(data)

    def validateCurrentPage(self) -> bool:  # noqa: N802 - Qt override
        if self.currentPage() is self.storage_page and not self.folders.paths():
            QMessageBox.warning(self, "Storage folder", "Add at least one storage folder.")
            return False
        if self.currentPage() is self.department_page:
            try:
                self.build_config()
            except ValidationError as exc:
                QMessageBox.warning(self, "Check these entries", explain_validation(exc))
                return False
        return super().validateCurrentPage()

    def accept(self) -> None:
        try:
            self.result_config = self.build_config()
        except ValidationError as exc:
            QMessageBox.warning(self, "Check these entries", explain_validation(exc))
            return
        super().accept()

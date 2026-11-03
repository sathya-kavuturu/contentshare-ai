"""A list of folder paths the user can add to, edit and remove from.

Used for the storage folders in the first-run wizard and in Settings, so both behave the same.
"""

from __future__ import annotations

import ntpath

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from contentshare.ui.widgets import Label as QLabel


def _same(path: str) -> str:
    """Two spellings of one folder (case, slashes, a trailing backslash) compare equal."""
    return ntpath.normcase(ntpath.normpath(path)) if path.strip() else ""


class FolderList(QWidget):
    """Folder paths with Add, Edit and Remove.

    * **Add folder…** picks a folder in the usual Windows dialog; the box beside it takes a typed
      or pasted path (a network path such as ``\\\\storage01\\Programs`` works too).
    * **Edit** (or a double-click, or F2) changes the selected path in place.
    * **Remove** (or the Delete key) takes the selected path out. Nothing on disk is touched.
    """

    def __init__(self, parent: QWidget | None = None, *, dialog_title: str = "Choose a folder"):
        super().__init__(parent)
        self._dialog_title = dialog_title
        self.list = QListWidget()
        self.list.setAccessibleName("Folders")
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.list.itemChanged.connect(self._edited)
        self.list.itemSelectionChanged.connect(self._update_buttons)
        self.list.model().rowsInserted.connect(lambda *_a: self._update_buttons())
        self.list.model().rowsRemoved.connect(lambda *_a: self._update_buttons())
        delete = QShortcut(QKeySequence(QKeySequence.StandardKey.Delete), self.list)
        delete.setContext(Qt.ShortcutContext.WidgetShortcut)
        delete.activated.connect(self.remove_selected)

        self.browse = QPushButton("Add folder…")
        self.browse.clicked.connect(self._browse)
        self.typed = QLineEdit()
        self.typed.setAccessibleName("Type or paste a folder path")
        self.typed.setPlaceholderText(r"or type a path: \\storage01\Programs")
        self.typed.returnPressed.connect(self.add_typed)
        self.add_button = QPushButton("Add")
        self.add_button.clicked.connect(self.add_typed)
        self.edit_button = QPushButton("Edit")
        self.edit_button.clicked.connect(self.edit_selected)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self.remove_selected)

        add_row = QHBoxLayout()
        add_row.addWidget(self.browse)
        add_row.addWidget(self.typed, 1)
        add_row.addWidget(self.add_button)
        change_row = QHBoxLayout()
        change_row.addWidget(self.edit_button)
        change_row.addWidget(self.remove_button)
        change_row.addStretch(1)
        self.note = QLabel("")
        self.note.setProperty("role", "muted")
        self.note.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.list, 1)
        layout.addLayout(add_row)
        layout.addLayout(change_row)
        layout.addWidget(self.note)
        self._update_buttons()

    # ------------------------------------------------------------------ reading and writing
    def paths(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]

    def set_paths(self, paths: list[str]) -> None:
        self.list.clear()
        self.note.setText("")
        for path in paths:
            self.add(path)

    def add(self, path: str) -> bool:
        """Add ``path`` unless it is empty or already listed. Returns whether it was added."""
        path = path.strip()
        if not path:
            return False
        if _same(path) in {_same(p) for p in self.paths()}:
            self.note.setText(f"{path} is already in the list.")
            return False
        self.note.setText("")
        item = QListWidgetItem(path)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        self.list.addItem(item)
        self.list.setCurrentItem(item)
        return True

    # ------------------------------------------------------------------ actions
    def add_typed(self) -> None:
        if self.add(self.typed.text()):
            self.typed.clear()

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, self._dialog_title)
        if chosen:
            self.add(chosen.replace("/", "\\"))

    def edit_selected(self) -> None:
        item = self.list.currentItem()
        if item is not None:
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            self.list.editItem(item)

    def remove_selected(self) -> None:
        row = self.list.currentRow()
        if row >= 0:
            self.list.takeItem(row)
            self.note.setText("")
            if self.list.count():
                self.list.setCurrentRow(min(row, self.list.count() - 1))

    def _edited(self, item: QListWidgetItem) -> None:
        """After an in-place edit: tidy the text, and drop an emptied or duplicated entry."""
        text = item.text().strip()
        others = {_same(self.list.item(i).text()) for i in range(self.list.count())
                  if self.list.item(i) is not item}  # fmt: skip
        if text and _same(text) not in others:
            if text != item.text():
                with_blocked = self.list.blockSignals(True)
                item.setText(text)
                self.list.blockSignals(with_blocked)
            return
        self.note.setText(
            "An empty path was removed." if not text else f"{text} is already in the list."
        )
        self.list.takeItem(self.list.row(item))

    def _update_buttons(self) -> None:
        has_selection = self.list.currentRow() >= 0 and bool(self.list.selectedItems())
        self.edit_button.setEnabled(has_selection)
        self.remove_button.setEnabled(has_selection)

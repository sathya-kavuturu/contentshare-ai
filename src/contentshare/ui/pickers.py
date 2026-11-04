"""Folder and file pickers that open quickly.

With no start folder Windows reopens the picker where it was last used. After someone has
browsed a network share that is often a server that is slow or switched off, and the picker
then waits for it before showing anything. These helpers always start in a local folder.
"""

from __future__ import annotations

from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import QFileDialog, QWidget


def _local(location: QStandardPaths.StandardLocation) -> str:
    return QStandardPaths.writableLocation(location)


def choose_folder(parent: QWidget | None, title: str) -> str:
    """The chosen folder with Windows slashes, or an empty string if the person cancelled."""
    chosen = QFileDialog.getExistingDirectory(
        parent,
        title,
        _local(QStandardPaths.StandardLocation.HomeLocation),
        QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontResolveSymlinks,
    )
    return chosen.replace("/", "\\")


def choose_file(parent: QWidget | None, title: str, name_filter: str) -> str:
    """The chosen file with Windows slashes, or an empty string if the person cancelled."""
    chosen, _filter = QFileDialog.getOpenFileName(
        parent, title, _local(QStandardPaths.StandardLocation.DownloadLocation), name_filter
    )
    return chosen.replace("/", "\\")

"""The in-app guide: how to connect Gmail, how to use the app, and what every setting does.

The text lives in ``contentshare/help/*.md`` (shipped inside the program, so it works offline).
Only our own files are shown, links open in the browser only when they are ``https://``, and
nothing from an email is ever put on this screen.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

import contentshare
from contentshare.ui.widgets import Label as QLabel

log = logging.getLogger(__name__)

HELP_DIR = Path(contentshare.__file__).resolve().parent / "help"
_FILE = re.compile(r"^(\d\d)-([a-z]+)\.md$")


@dataclass(frozen=True)
class Topic:
    slug: str  # "gmail": what a link such as help:gmail points at
    title: str
    markdown: str


def load_topics(folder: Path = HELP_DIR) -> list[Topic]:
    """The topics in file-name order. A missing or unreadable file is skipped (and logged)."""
    topics: list[Topic] = []
    for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        match = _FILE.match(path.name)
        if match is None:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("help topic %s could not be read: %s", path.name, type(exc).__name__)
            continue
        first = text.lstrip().splitlines()[0] if text.strip() else ""
        title = first.lstrip("# ").strip() or match[2].title()
        topics.append(Topic(match[2], title, text))
    return topics


class _Browser(QTextBrowser):
    """Shows Markdown; never loads anything from the network or the disk."""

    def loadResource(self, type: int, name: QUrl | str) -> object:  # noqa: N802 - Qt override
        return None


class HelpPage(QWidget):
    def __init__(self, topics: list[Topic] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("page")
        self.topics = load_topics() if topics is None else topics

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 12)
        title = QLabel("Help")
        title.setProperty("role", "title")
        root.addWidget(title)

        body = QHBoxLayout()
        self.topic_list = QListWidget()
        self.topic_list.setAccessibleName("Help topics")
        self.topic_list.setFixedWidth(210)
        self.topic_list.setSpacing(3)
        for topic in self.topics:
            self.topic_list.addItem(QListWidgetItem(topic.title))
        body.addWidget(self.topic_list)

        self.browser = _Browser()
        self.browser.setAccessibleName("Help text")
        self.browser.setOpenLinks(False)  # we decide what a link does
        self.browser.anchorClicked.connect(self._link)
        body.addWidget(self.browser, 1)
        root.addLayout(body, 1)

        self.version = QLabel(f"ContentShare {contentshare.__version__}")
        self.version.setProperty("role", "muted")
        root.addWidget(self.version)

        self.topic_list.currentRowChanged.connect(self._show_row)
        if self.topics:
            self.topic_list.setCurrentRow(0)
        else:
            self.browser.setPlainText("The help text could not be found. Reinstall ContentShare.")

    # ------------------------------------------------------------------ showing
    def show_topic(self, slug: str) -> bool:
        for row, topic in enumerate(self.topics):
            if topic.slug == slug:
                self.topic_list.setCurrentRow(row)
                return True
        return False

    def current_slug(self) -> str:
        row = self.topic_list.currentRow()
        return self.topics[row].slug if 0 <= row < len(self.topics) else ""

    def restyle(self) -> None:
        """After the theme changed: the text view keeps the link colour it was created with."""
        self.browser.setPalette(QApplication.palette())
        self._show_row(self.topic_list.currentRow())

    def _show_row(self, row: int) -> None:
        if 0 <= row < len(self.topics):
            self.browser.setMarkdown(self.topics[row].markdown)
            self.browser.verticalScrollBar().setValue(0)

    def _link(self, url: QUrl) -> None:
        if url.scheme() == "help":
            self.show_topic(url.path() or url.host())
        elif url.scheme() == "https":
            QDesktopServices.openUrl(url)
        # anything else (file:, http:, javascript:, …) is ignored

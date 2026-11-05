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
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QTextDocument,
    QTextFrameFormat,
    QTextTable,
)
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
from contentshare.ui.theme import MONO_STACK, Theme, current_theme
from contentshare.ui.widgets import Label as QLabel
from contentshare.ui.widgets import page_header

log = logging.getLogger(__name__)

HELP_DIR = Path(contentshare.__file__).resolve().parent / "help"
_FILE = re.compile(r"^(\d\d)-([a-z]+)\.md$")


@dataclass(frozen=True)
class Topic:
    slug: str  # "gmail": what a link such as help:gmail points at
    title: str
    markdown: str


def load_topics(folder: Path = HELP_DIR) -> list[Topic]:
    """The topics in file-name order. A missing or unreadable file is skipped (and logged).

    Two files for one topic (``04-settings.md`` left behind by an older version next to
    ``05-settings.md``) show once: the one with the higher number is the newer."""
    found: dict[str, tuple[str, Topic]] = {}  # slug -> (file name, topic)
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
        found[match[2]] = (path.name, Topic(match[2], title, text))  # a later file wins
    return [topic for _name, topic in sorted(found.values(), key=lambda item: item[0])]


def help_css(t: Theme) -> str:
    """How the Markdown is drawn: the same colours and sizes as the rest of the program."""
    return f"""
    body {{ color: {t.text}; font-size: 14px; }}
    h1 {{ font-size: 24px; font-weight: 600; color: {t.text}; margin-top: 0; margin-bottom: 6px; }}
    h2 {{ font-size: 17px; font-weight: 600; color: {t.text}; margin-top: 22px;
        margin-bottom: 4px; }}
    h3 {{ font-size: 14px; font-weight: 600; color: {t.text}; margin-top: 16px; }}
    p, li {{ color: {t.text}; font-size: 14px; }}
    li {{ margin-bottom: 3px; }}
    a {{ color: {t.link}; text-decoration: none; }}
    code {{ font-family: {MONO_STACK}; font-size: 13px; background-color: {t.window};
        color: {t.text}; }}
    em {{ color: {t.muted}; }}
    table {{ border-color: {t.border}; border-collapse: collapse; }}
    th {{ background-color: {t.window}; color: {t.muted}; font-size: 13px; font-weight: 600;
        padding: 6px 10px; }}
    td {{ padding: 6px 10px; border-color: {t.border}; }}
    """


def _style_tables(document: QTextDocument, t: Theme) -> None:
    """Markdown tables come out with black borders; give them hairlines and some air."""
    for frame in document.rootFrame().childFrames():
        if not isinstance(frame, QTextTable):
            continue
        fmt = frame.format()
        fmt.setBorder(1)
        fmt.setBorderBrush(QColor(t.border))
        fmt.setBorderStyle(QTextFrameFormat.BorderStyle.BorderStyle_Solid)
        fmt.setCellPadding(8)
        fmt.setCellSpacing(0)
        frame.setFormat(fmt)
        for column in range(frame.columns()):
            cell = frame.cellAt(0, column)
            cell_fmt = cell.format()
            cell_fmt.setBackground(QColor(t.window))
            cell.setFormat(cell_fmt)


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
        root.setContentsMargins(32, 28, 26, 16)
        root.setSpacing(14)
        root.addLayout(
            page_header("Help", "Connect Gmail, use the app, and what every setting does.")
        )

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
            self.browser.document().setDefaultStyleSheet(help_css(current_theme()))
            self.browser.setMarkdown(self.topics[row].markdown)
            _style_tables(self.browser.document(), current_theme())
            self.browser.verticalScrollBar().setValue(0)

    def _link(self, url: QUrl) -> None:
        if url.scheme() == "help":
            self.show_topic(url.path() or url.host())
        elif url.scheme() == "https":
            QDesktopServices.openUrl(url)
        # anything else (file:, http:, javascript:, …) is ignored

"""The list of screens on the left: an icon, the name, and a count pill when something waits.

Folded (see ``NavList.set_collapsed``) it shows only the icons, with the name as a tooltip."""

from __future__ import annotations

import re

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QListWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)

from contentshare.ui.theme import current_theme

ROW_HEIGHT = 38
_COUNT = re.compile(
    r"^(.*?)\s*\((\d+)\)$"
)  # "Requests (3)": the text stays plain for screen readers
_GLYPHS = {  # Segoe Fluent Icons / Segoe MDL2 Assets code points
    "Requests": "",
    "Active copies": "",
    "History": "",
    "Mailbox": "",  # the envelope
    "Settings": "",
    "Help": "",
    "Developer": "",  # the code brackets
}
_ICON_FONTS = ("Segoe Fluent Icons", "Segoe MDL2 Assets")


def icon_font_family() -> str | None:
    families = set(QFontDatabase.families())
    return next((f for f in _ICON_FONTS if f in families), None)


def split_count(text: str) -> tuple[str, int]:
    """``"Requests (3)"`` -> ``("Requests", 3)``; text without a count gives 0."""
    match = _COUNT.match(text)
    return (match[1], int(match[2])) if match else (text, 0)


class NavDelegate(QStyledItemDelegate):
    collapsed = False  # icons only

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        t = current_theme()
        name, count = split_count(str(index.data(Qt.ItemDataRole.DisplayRole) or ""))
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(option.rect).adjusted(10, 2, -10, -2)
        if selected or hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(t.accent_soft if selected else t.hover))
            painter.drawRoundedRect(box, 8, 8)
        ink = QColor(t.text if selected else t.muted)

        icon_family = icon_font_family()
        if self.collapsed:
            self._paint_folded(painter, box, name, count, selected, icon_family)
            painter.restore()
            return
        left = box.left() + 12
        if icon_family and name in _GLYPHS:
            icon_font = QFont(icon_family)
            icon_font.setPixelSize(16)
            painter.setFont(icon_font)
            painter.setPen(QColor(t.accent if selected else t.muted))
            painter.drawText(
                QRectF(left, box.top(), 20, box.height()),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                _GLYPHS[name],
            )
            left += 28

        font = QFont(option.font)
        font.setWeight(QFont.Weight.DemiBold if selected else QFont.Weight.Medium)
        painter.setFont(font)
        painter.setPen(ink)
        painter.drawText(
            QRectF(left, box.top(), box.right() - left - 8, box.height()),
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            name,
        )

        if count:
            pill_font = QFont(option.font)
            pill_font.setPixelSize(11)
            pill_font.setWeight(QFont.Weight.Bold)
            painter.setFont(pill_font)
            text = str(count) if count < 100 else "99+"
            width = max(22, QFontMetrics(pill_font).horizontalAdvance(text) + 14)
            pill = QRectF(box.right() - width - 10, box.center().y() - 9, width, 18)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(t.accent))
            painter.drawRoundedRect(pill, 9, 9)
            painter.setPen(QColor(t.accent_text))
            painter.drawText(pill, int(Qt.AlignmentFlag.AlignCenter), text)
        painter.restore()

    @staticmethod
    def _paint_folded(
        painter: QPainter,
        box: QRectF,
        name: str,
        count: int,
        selected: bool,
        icon_family: str | None,
    ) -> None:
        t = current_theme()
        centre = int(Qt.AlignmentFlag.AlignCenter)
        if icon_family and name in _GLYPHS:
            font = QFont(icon_family)
            font.setPixelSize(18)
            text = _GLYPHS[name]
        else:  # no icon font on this PC: the first letter
            font = QFont()
            font.setPixelSize(15)
            font.setWeight(QFont.Weight.DemiBold)
            text = name[:1]
        painter.setFont(font)
        painter.setPen(QColor(t.accent if selected else t.muted))
        painter.drawText(box, centre, text)
        if count:
            pill_font = QFont()
            pill_font.setPixelSize(10)
            pill_font.setWeight(QFont.Weight.Bold)
            painter.setFont(pill_font)
            label = str(count) if count < 100 else "99+"
            width = max(16, QFontMetrics(pill_font).horizontalAdvance(label) + 10)
            pill = QRectF(box.right() - width, box.top() + 2, width, 14)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(t.accent))
            painter.drawRoundedRect(pill, 7, 7)
            painter.setPen(QColor(t.accent_text))
            painter.drawText(pill, centre, label)

    def sizeHint(  # noqa: N802 - Qt override
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QSize:
        return QSize(option.rect.width(), ROW_HEIGHT)


class NavList(QListWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("nav")
        self.setItemDelegate(NavDelegate(self))
        self.setMouseTracking(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._delegate = self.itemDelegate()

    def set_collapsed(self, collapsed: bool) -> None:
        """Show only the icons (and a small count), or the full names again."""
        delegate = self.itemDelegate()
        if isinstance(delegate, NavDelegate):
            delegate.collapsed = collapsed
        self.refresh_tooltips()
        self.viewport().update()

    def refresh_tooltips(self) -> None:
        """Folded, the name (and count) is the only label there is."""
        delegate = self.itemDelegate()
        folded = isinstance(delegate, NavDelegate) and delegate.collapsed
        for row in range(self.count()):
            item = self.item(row)
            if item is not None:
                item.setToolTip(item.text() if folded else "")

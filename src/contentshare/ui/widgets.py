"""Small shared widgets."""

from __future__ import annotations

import shiboken6
from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
)
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPaintEvent, QPen, QResizeEvent
from PySide6.QtWidgets import (
    QComboBox,
    QLabel,
    QListView,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from contentshare.ui.theme import current_theme


class Label(QLabel):
    """A label that shows its text exactly as given.

    Qt's default (``AutoText``) renders text that looks like HTML as HTML. Email subjects,
    names and file names are written by other people, and rich text can load images from a
    network address, which makes Windows send the user's sign-in hash to that server. Every
    label in this program therefore uses this class, and no text is ever rich.
    """

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setTextFormat(Qt.TextFormat.PlainText)


class Combo(QComboBox):
    """A drop-down whose list is laid out by the style sheet.

    The default list ignores the item padding and height in the style sheet, so the choices
    sat tight against each other and the edge. A plain ``QListView`` as the list honours them.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setView(QListView())
        self.setMaxVisibleItems(12)

    def showPopup(self) -> None:  # noqa: N802 - Qt override
        # At least as wide as the box, and wide enough for the longest choice plus its padding.
        longest = max(
            (self.fontMetrics().horizontalAdvance(self.itemText(i)) for i in range(self.count())),
            default=0,
        )
        self.view().setMinimumWidth(max(self.width(), longest + 48))
        super().showPopup()


def page_header(title: str, subtitle: str = "") -> QVBoxLayout:
    """The big title of a screen with a quiet line under it saying what the screen is for."""
    block = QVBoxLayout()
    block.setSpacing(2)
    heading = Label(title)
    heading.setProperty("role", "title")
    block.addWidget(heading)
    if subtitle:
        line = Label(subtitle)
        line.setProperty("role", "muted")
        block.addWidget(line)
    return block


class ElidedLabel(Label):
    """Text on one line that is shortened in the middle when it does not fit; the full text is
    the tooltip.

    A path has no spaces to wrap at, so a long one used to make its whole card wider than the
    window. A label that never wraps also has a fixed height, which keeps the height of a card
    that is made of such rows easy to work out.
    """

    def __init__(
        self, text: str = "", role: str | None = None, parent: QWidget | None = None
    ) -> None:
        super().__init__(text, parent)
        if role:
            self.setProperty("role", role)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setToolTip(text)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt override
        super().setText(text)
        self.setToolTip(text)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        self.ensurePolished()  # the font comes from the style sheet
        return QSize(60, QFontMetrics(self.font()).height() + 6)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(40, self.sizeHint().height())

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setPen(self.palette().windowText().color())
        painter.setFont(self.font())
        shown = QFontMetrics(self.font()).elidedText(
            self.text(), Qt.TextElideMode.ElideMiddle, self.width()
        )
        painter.drawText(
            self.rect(), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), shown
        )
        painter.end()


class PathLabel(ElidedLabel):
    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, "path", parent)


class SafeMessageBox(QMessageBox):
    """``QMessageBox`` whose static helpers show plain text only (same reason as ``Label``)."""

    @staticmethod
    def _run(
        icon: QMessageBox.Icon,
        parent: QWidget | None,
        title: str,
        text: str,
        buttons: QMessageBox.StandardButton,
        default: QMessageBox.StandardButton,
    ) -> QMessageBox.StandardButton:
        box = QMessageBox(icon, title, text, buttons, parent)
        box.setTextFormat(Qt.TextFormat.PlainText)
        if default != QMessageBox.StandardButton.NoButton:
            box.setDefaultButton(default)
        return QMessageBox.StandardButton(box.exec())

    @staticmethod
    def information(
        parent: QWidget | None,
        title: str,
        text: str,
        buttons: QMessageBox.StandardButton = QMessageBox.StandardButton.Ok,
        defaultButton: QMessageBox.StandardButton = QMessageBox.StandardButton.NoButton,  # noqa: N803
    ) -> QMessageBox.StandardButton:
        return SafeMessageBox._run(
            QMessageBox.Icon.Information, parent, title, text, buttons, defaultButton
        )

    @staticmethod
    def warning(
        parent: QWidget | None,
        title: str,
        text: str,
        buttons: QMessageBox.StandardButton = QMessageBox.StandardButton.Ok,
        defaultButton: QMessageBox.StandardButton = QMessageBox.StandardButton.NoButton,  # noqa: N803
    ) -> QMessageBox.StandardButton:
        return SafeMessageBox._run(
            QMessageBox.Icon.Warning, parent, title, text, buttons, defaultButton
        )

    @staticmethod
    def critical(
        parent: QWidget | None,
        title: str,
        text: str,
        buttons: QMessageBox.StandardButton = QMessageBox.StandardButton.Ok,
        defaultButton: QMessageBox.StandardButton = QMessageBox.StandardButton.NoButton,  # noqa: N803
    ) -> QMessageBox.StandardButton:
        return SafeMessageBox._run(
            QMessageBox.Icon.Critical, parent, title, text, buttons, defaultButton
        )

    @staticmethod
    def question(
        parent: QWidget | None,
        title: str,
        text: str,
        buttons: QMessageBox.StandardButton = (
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ),
        defaultButton: QMessageBox.StandardButton = QMessageBox.StandardButton.NoButton,  # noqa: N803
    ) -> QMessageBox.StandardButton:
        return SafeMessageBox._run(
            QMessageBox.Icon.Question, parent, title, text, buttons, defaultButton
        )


class StackBody(QWidget):
    """The inside of a scroll area that stacks cards vertically.

    Cards contain word-wrapped text, whose height depends on the width. A plain layout inside
    a scroll area squeezes such cards below their text. This body gives every card the height
    its text needs at the current width, so the area scrolls instead.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.stack = QVBoxLayout(self)
        self.stack.setContentsMargins(0, 0, 6, 0)
        self.stack.setSpacing(12)
        self.stack.addStretch(1)

    def add(self, widget: QWidget) -> None:
        self.stack.insertWidget(self.stack.count() - 1, widget)
        widget.installEventFilter(self)  # a card that folds or unfolds changes its own height
        self.fit()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        if event.type() == QEvent.Type.LayoutRequest:
            QTimer.singleShot(0, self.fit)  # after the card has laid itself out
        return super().eventFilter(watched, event)

    def remove(self, widget: QWidget) -> None:
        self.stack.removeWidget(widget)
        widget.hide()  # it is deleted a moment later; until then it must not show through
        widget.deleteLater()

    def fit(self) -> None:
        if not shiboken6.isValid(self) or not shiboken6.isValid(self.stack):
            return  # a deferred call that arrives after the window was closed
        margins = self.stack.contentsMargins()
        width = max(200, self.width() - margins.left() - margins.right())
        for i in range(self.stack.count()):
            item = self.stack.itemAt(i)
            child = item.widget() if item is not None else None
            layout = child.layout() if child is not None else None
            if child is not None and layout is not None:
                # Qt under-reports the wrapped height of nested layouts, so never go below
                # the layout's own preferred height, nor below what the card asks for itself.
                needed = max(layout.totalHeightForWidth(width), child.sizeHint().height())
                if child.minimumHeight() != needed:
                    child.setMinimumHeight(needed)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self.fit()


class BurgerButton(QPushButton):
    """The menu icon that folds the menu on the left. It is drawn here, in the theme's colours,
    so it is sharp at any scale and in both themes. Three bars (the menu is folded: press to open
    it) turn smoothly into an arrow pointing left (the menu is open: press to fold it)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(40, 36)
        self.setFlat(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)  # a keyboard ring, none after a mouse click
        self._shape = 1.0  # 0: three bars, 1: arrow
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(MENU_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._anim.valueChanged.connect(self._set_shape)

    def _set_shape(self, value: object) -> None:
        self._shape = float(value) if isinstance(value, int | float) else 0.0
        self.update()

    def show_open(self, is_open: bool, *, animate: bool = False) -> None:
        """The menu is open (arrow) or folded (bars); with ``animate`` the icon turns over."""
        target = 1.0 if is_open else 0.0
        self._anim.stop()
        if animate:
            self._anim.setStartValue(self._shape)
            self._anim.setEndValue(target)
            self._anim.start()
        else:
            self._set_shape(target)

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802 (Qt name)
        theme = current_theme()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self.isDown() or self.underMouse():
            painter.setBrush(QColor(theme.border if self.isDown() else theme.hover))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(box, 8, 8)
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(theme.accent), 2))
            painter.drawRoundedRect(box, 8, 8)
        pen = QPen(QColor(theme.text), 2.2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        cx, cy, t = self.width() / 2, self.height() / 2, self._shape
        for (bx1, by1, bx2, by2), (ax1, ay1, ax2, ay2) in _BURGER_LINES:
            painter.drawLine(
                QPointF(cx + bx1 + (ax1 - bx1) * t, cy + by1 + (ay1 - by1) * t),
                QPointF(cx + bx2 + (ax2 - bx2) * t, cy + by2 + (ay2 - by2) * t),
            )


MENU_MS = 220  # how long the menu takes to fold or open
# each line: (as a bar, as part of the arrow), the points relative to the middle of the button
_BURGER_LINES = (
    ((-9.0, -6.0, 9.0, -6.0), (2.0, -7.0, -6.0, 0.0)),
    ((-9.0, 0.0, 9.0, 0.0), (-6.0, 0.0, 8.0, 0.0)),
    ((-9.0, 6.0, 9.0, 6.0), (-6.0, 0.0, 2.0, 7.0)),
)

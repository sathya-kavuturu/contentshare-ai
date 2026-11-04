"""Small shared widgets."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QFontMetrics, QPainter, QPaintEvent, QResizeEvent
from PySide6.QtWidgets import (
    QComboBox,
    QLabel,
    QListView,
    QMessageBox,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


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
        self.fit()

    def remove(self, widget: QWidget) -> None:
        self.stack.removeWidget(widget)
        widget.deleteLater()

    def fit(self) -> None:
        margins = self.stack.contentsMargins()
        width = max(200, self.width() - margins.left() - margins.right())
        for i in range(self.stack.count()):
            item = self.stack.itemAt(i)
            child = item.widget() if item is not None else None
            layout = child.layout() if child is not None else None
            if child is not None and layout is not None:
                # Qt under-reports the wrapped height of nested layouts, so never go below
                # the layout's own preferred height.
                child.setMinimumHeight(layout.totalHeightForWidth(width))

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self.fit()

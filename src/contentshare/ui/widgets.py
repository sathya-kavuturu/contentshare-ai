"""Small shared widgets."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import QLabel, QMessageBox, QVBoxLayout, QWidget


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
        self.stack.setContentsMargins(0, 0, 8, 0)
        self.stack.setSpacing(10)
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
                child.setMinimumHeight(
                    max(layout.totalHeightForWidth(width), layout.sizeHint().height())
                )

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self.fit()

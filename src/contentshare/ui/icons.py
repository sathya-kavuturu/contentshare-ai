"""The application icon, drawn in code so there is no image file to ship or to go missing."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QIcon, QPainter, QPen, QPixmap, QPolygonF

BLUE = QColor("#5e6ad2")
WHITE = QColor("#ffffff")
AMBER = QColor("#f59e0b")


def _draw(size: int, attention: bool) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(BLUE))
    radius = size * 0.22
    p.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)

    # A page with a check mark: content that was copied and verified.
    s = size / 32.0
    p.setBrush(QBrush(WHITE))
    page = QPolygonF(
        [
            QPointF(9 * s, 6 * s),
            QPointF(18 * s, 6 * s),
            QPointF(23 * s, 11 * s),
            QPointF(23 * s, 26 * s),
            QPointF(9 * s, 26 * s),
        ]
    )
    p.drawPolygon(page)
    pen = QPen(BLUE, max(1.5, 2.2 * s), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.drawPolyline(QPolygonF([QPointF(12.5 * s, 17 * s), QPointF(15.5 * s, 20.5 * s),
                              QPointF(20 * s, 14 * s)]))  # fmt: skip
    if attention:  # something is waiting: a dot in the corner, drawn with a ring to stand out
        p.setPen(QPen(WHITE, max(1.0, 1.6 * s)))
        p.setBrush(QBrush(AMBER))
        d = size * 0.42
        p.drawEllipse(QRectF(size - d, 0, d, d))
    p.end()
    return pixmap


def app_icon(*, attention: bool = False) -> QIcon:
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(_draw(size, attention))
    return icon

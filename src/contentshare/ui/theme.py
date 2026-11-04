"""Colours and the application style sheet, in a light and a dark variant.

Status is always shown as icon + word + colour, never colour alone (see ``status.py``); the
colours below keep a contrast ratio of at least 4.5:1 between text and its background.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QApplication

THEME_PROPERTY = "contentshare_theme"  # which theme the application is showing now


@dataclass(frozen=True)
class Tone:
    fg: str
    bg: str


@dataclass(frozen=True)
class Theme:
    name: str
    window: str
    card: str
    border: str
    text: str
    muted: str
    accent: str
    accent_text: str
    input_bg: str
    banner: Tone
    tones: dict[str, Tone]


LIGHT = Theme(
    name="light", window="#F3F4F6", card="#FFFFFF", border="#D5D8DD", text="#1F2328",
    muted="#59626C", accent="#0B5CAD", accent_text="#FFFFFF", input_bg="#FFFFFF",
    banner=Tone("#6B3E00", "#FFE8B3"),
    tones={
        "ready": Tone("#0B6B2B", "#E1F4E7"),
        "warn": Tone("#7A4300", "#FFEFCF"),
        "error": Tone("#A4262C", "#FCE4E6"),
        "info": Tone("#0F548C", "#DFEEFB"),
        "neutral": Tone("#4A4F55", "#E9EAEC"),
    },
)  # fmt: skip
DARK = Theme(
    name="dark", window="#16181B", card="#202328", border="#3A3F46", text="#E6E8EB",
    muted="#A3ABB5", accent="#4DA3F0", accent_text="#0B1620", input_bg="#1B1E22",
    banner=Tone("#FFD98A", "#4A3508"),
    tones={
        "ready": Tone("#7FD99A", "#17331F"),
        "warn": Tone("#F2C46D", "#3A2C0E"),
        "error": Tone("#FF9AA2", "#3F1A1D"),
        "info": Tone("#8CC4F5", "#14293D"),
        "neutral": Tone("#C8CDD3", "#2F333A"),
    },
)  # fmt: skip


def detect_theme(app: QApplication) -> Theme:
    scheme = app.styleHints().colorScheme()
    return DARK if scheme == Qt.ColorScheme.Dark else LIGHT


def theme_for(appearance: str, app: QApplication) -> Theme:
    """The theme for the Appearance setting: "light", "dark", or "system" (follow Windows)."""
    if appearance == "dark":
        return DARK
    if appearance == "light":
        return LIGHT
    return detect_theme(app)


def check_icon(t: Theme) -> Path:
    """A white tick on transparent, drawn once per theme (style sheets need an image file),
    so a ticked box differs from an empty one by shape and not only by colour."""
    folder = Path(tempfile.gettempdir()) / "contentshare-ui"
    folder.mkdir(exist_ok=True)
    path = folder / f"check-{t.name}.png"
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(t.accent_text))
    pen.setWidth(4)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.drawPolyline([QPoint(8, 17), QPoint(14, 23), QPoint(24, 10)])
    painter.end()
    pixmap.save(str(path))
    return path


def stylesheet(t: Theme, check: Path | None = None) -> str:
    check_rule = f"image: url({check.as_posix()});" if check else ""
    tone_rules = "\n".join(
        f'QLabel[tone="{name}"] {{ color: {tone.fg}; background: {tone.bg}; '
        f"border-radius: 9px; padding: 2px 10px; font-weight: 600; }}"
        for name, tone in t.tones.items()
    )
    return f"""
* {{ font-family: "Segoe UI", "Segoe UI Symbol", sans-serif; font-size: 10pt; }}
QMainWindow, QDialog, QWizard, QWidget#page {{ background: {t.window}; color: {t.text}; }}
QLabel {{ color: {t.text}; }}
QLabel[role="muted"] {{ color: {t.muted}; }}
QLabel[role="title"] {{ font-size: 17pt; font-weight: 600; }}
QLabel[role="path"] {{ font-family: Consolas, "Cascadia Mono", monospace; font-size: 9.5pt; }}
{tone_rules}
QLabel#banner {{ color: {t.banner.fg}; background: {t.banner.bg}; padding: 6px 14px;
    font-weight: 600; }}
QFrame#card {{ background: {t.card}; border: 1px solid {t.border}; border-radius: 8px; }}
QFrame#sidebar {{ background: {t.card}; border-right: 1px solid {t.border}; }}
QListWidget#nav {{ background: transparent; border: none; outline: none; }}
QListWidget#nav::item {{ padding: 10px 16px; border-radius: 6px; margin: 2px 8px; }}
QListWidget#nav::item:selected {{ background: {t.accent}; color: {t.accent_text}; }}
QListWidget#nav::item:hover:!selected {{ background: {t.window}; }}
QPushButton {{ background: {t.card}; color: {t.text}; border: 1px solid {t.border};
    border-radius: 6px; padding: 6px 14px; }}
QPushButton:hover {{ border-color: {t.accent}; }}
QPushButton:focus {{ border: 2px solid {t.accent}; padding: 5px 13px; }}
QPushButton:disabled {{ color: {t.muted}; background: {t.window}; }}
QPushButton[primary="true"] {{ background: {t.accent}; color: {t.accent_text};
    border: 1px solid {t.accent}; font-weight: 600; }}
QPushButton[primary="true"]:disabled {{ background: {t.border}; color: {t.muted};
    border-color: {t.border}; }}
QPushButton[danger="true"] {{ color: {t.tones["error"].fg}; }}
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QTimeEdit, QListWidget, QTableWidget {{
    background: {t.input_bg}; color: {t.text}; border: 1px solid {t.border}; border-radius: 5px;
    padding: 4px 6px; selection-background-color: {t.accent}; selection-color: {t.accent_text}; }}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus, QTimeEdit:focus {{
    border: 2px solid {t.accent}; }}
QTableWidget {{ gridline-color: {t.border}; }}
QHeaderView::section {{ background: {t.window}; color: {t.muted}; border: none;
    border-bottom: 1px solid {t.border}; padding: 6px; font-weight: 600; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border: 2px solid {t.muted};
    border-radius: 4px; background: {t.input_bg}; }}
QCheckBox::indicator:hover {{ border-color: {t.accent}; }}
QCheckBox::indicator:checked {{ background: {t.accent}; border-color: {t.accent};
    {check_rule} }}
QCheckBox::indicator:disabled {{ border-color: {t.border}; }}
QProgressBar {{ background: {t.window}; border: 1px solid {t.border}; border-radius: 6px;
    height: 14px; text-align: center; color: {t.text}; }}
QProgressBar::chunk {{ background: {t.accent}; border-radius: 5px; }}
QTabWidget::pane {{ border: 1px solid {t.border}; border-radius: 6px; background: {t.card}; }}
QTabBar::tab {{ padding: 8px 16px; color: {t.muted}; border: none; background: transparent; }}
QTabBar::tab:selected {{ color: {t.text}; border-bottom: 2px solid {t.accent}; }}
QScrollArea {{ border: none; background: transparent; }}
QStatusBar {{ background: {t.card}; color: {t.muted}; border-top: 1px solid {t.border}; }}
QGroupBox {{ border: 1px solid {t.border}; border-radius: 6px; margin-top: 14px; padding: 10px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {t.muted}; }}
"""


def palette_for(t: Theme) -> QPalette:
    palette = QPalette()
    for role, color in (
        (QPalette.ColorRole.Window, t.window), (QPalette.ColorRole.WindowText, t.text),
        (QPalette.ColorRole.Base, t.input_bg), (QPalette.ColorRole.AlternateBase, t.window),
        (QPalette.ColorRole.Text, t.text), (QPalette.ColorRole.Button, t.card),
        (QPalette.ColorRole.ButtonText, t.text), (QPalette.ColorRole.Highlight, t.accent),
        (QPalette.ColorRole.HighlightedText, t.accent_text),
        (QPalette.ColorRole.PlaceholderText, t.muted), (QPalette.ColorRole.ToolTipBase, t.card),
        (QPalette.ColorRole.ToolTipText, t.text),
        (QPalette.ColorRole.Link, t.accent), (QPalette.ColorRole.LinkVisited, t.accent),
    ):  # fmt: skip
        palette.setColor(role, QColor(color))
    return palette


def apply_theme(app: QApplication, theme: Theme | None = None) -> Theme:
    theme = theme or detect_theme(app)
    if app.property(THEME_PROPERTY) == theme.name:
        return theme  # already showing it; restyling every widget again would only be slow
    if app.property(THEME_PROPERTY) is None:  # once: setting a style again under a style sheet
        app.setStyle(
            "Fusion"
        )  # can crash Qt. Fusion is a consistent base on every Windows 11 setup
    app.setPalette(palette_for(theme))  # so checkboxes, menus and dialogs follow the theme too
    app.setStyleSheet(stylesheet(theme, check_icon(theme)))
    app.setProperty(THEME_PROPERTY, theme.name)
    return theme

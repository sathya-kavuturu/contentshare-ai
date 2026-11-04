"""Colours and the application style sheet, in a light and a dark variant.

The look follows the Linear design analysis in ``awesome-design-md``: a near-black canvas with
charcoal panels and hairline borders in dark, soft grey surfaces with white panels in light, a
single lavender accent (#5e6ad2) used only for the primary action, the selection and the focus
ring, and tight 6 to 10 px corners.

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
FONT_STACK = '"Segoe UI Variable Text", "Segoe UI", "Segoe UI Symbol", sans-serif'
MONO_STACK = '"Cascadia Mono", Consolas, monospace'


@dataclass(frozen=True)
class Tone:
    fg: str
    bg: str


@dataclass(frozen=True)
class Theme:
    name: str
    window: str  # the page behind everything
    sidebar: str
    card: str  # raised panels
    border: str  # hairlines
    border_strong: str  # outlines of inputs and buttons
    text: str
    muted: str
    subtle: str  # hints that may be quiet (never the only carrier of meaning)
    accent: str
    accent_hover: str
    accent_text: str  # text on the accent colour
    accent_soft: str  # a tint of the accent: selected rows
    link: str
    hover: str  # the background of a hovered row or flat button
    input_bg: str
    banner: Tone
    tones: dict[str, Tone]


LIGHT = Theme(
    name="light", window="#F6F7F8", sidebar="#FBFBFC", card="#FFFFFF", border="#E4E6EA",
    border_strong="#D3D6DC", text="#1B1D21", muted="#5B6370", subtle="#626A77", accent="#5E6AD2",
    accent_hover="#4F5BC4", accent_text="#FFFFFF", accent_soft="#ECEEFC", link="#4651B8",
    hover="#EFF0F3", input_bg="#FFFFFF",
    banner=Tone("#7A4A00", "#FFF3D9"),
    tones={
        "ready": Tone("#11693A", "#E3F5EA"),
        "warn": Tone("#8A5300", "#FFF1D6"),
        "error": Tone("#B3261E", "#FDE8E7"),
        "info": Tone("#3B47A8", "#E8EAFF"),
        "neutral": Tone("#4B5260", "#ECEEF1"),
    },
)  # fmt: skip
DARK = Theme(
    name="dark", window="#0C0D0E", sidebar="#0F1011", card="#141516", border="#23252A",
    border_strong="#34343A", text="#F7F8F8", muted="#9BA1AB", subtle="#8A8F98", accent="#5E6AD2",
    accent_hover="#7480E4", accent_text="#FFFFFF", accent_soft="#1D2040", link="#9AA5FF",
    hover="#1B1C1E", input_bg="#0F1011",
    banner=Tone("#F5C26B", "#2B2110"),
    tones={
        "ready": Tone("#5FD38D", "#10261A"),
        "warn": Tone("#F5C26B", "#2B2110"),
        "error": Tone("#FF8D8F", "#2E1517"),
        "info": Tone("#A3AEFF", "#1B1E3D"),
        "neutral": Tone("#C4C9D1", "#1F2124"),
    },
)  # fmt: skip

_shown: list[Theme] = [LIGHT]  # the one theme on screen (a list, so no global statement)


def current_theme() -> Theme:
    """The theme on screen now, for widgets that paint themselves."""
    return _shown[0]


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


@dataclass(frozen=True)
class Icons:
    """Small images the style sheet needs (it can only refer to files)."""

    check: Path
    down: Path
    up: Path


def _folder() -> Path:
    folder = Path(tempfile.gettempdir()) / "contentshare-ui"
    folder.mkdir(exist_ok=True)
    return folder


def _draw(path: Path, colour: str, points: list[QPoint], width: int) -> Path:
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(colour))
    pen.setWidth(width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.drawPolyline(points)
    painter.end()
    pixmap.save(str(path))
    return path


def make_icons(t: Theme) -> Icons:
    """A white tick (so a ticked box differs from an empty one by shape, not only colour) and the
    two chevrons of drop-downs and spin boxes, drawn once per theme."""
    folder = _folder()
    return Icons(
        check=_draw(
            folder / f"check-{t.name}.png", t.accent_text,
            [QPoint(8, 17), QPoint(14, 23), QPoint(24, 10)], 4,
        ),
        down=_draw(
            folder / f"down-{t.name}.png", t.muted,
            [QPoint(9, 13), QPoint(16, 20), QPoint(23, 13)], 3,
        ),
        up=_draw(
            folder / f"up-{t.name}.png", t.muted,
            [QPoint(9, 19), QPoint(16, 12), QPoint(23, 19)], 3,
        ),
    )  # fmt: skip


def check_icon(t: Theme) -> Path:
    return make_icons(t).check


def stylesheet(t: Theme, icons: Icons | None = None) -> str:
    check = f"image: url({icons.check.as_posix()});" if icons else ""
    down = f"image: url({icons.down.as_posix()});" if icons else ""
    up = f"image: url({icons.up.as_posix()});" if icons else ""
    tone_rules = "\n".join(
        f'QLabel[tone="{name}"] {{ color: {tone.fg}; background: {tone.bg}; border-radius: 10px; '
        f"padding: 2px 9px; font-size: 9pt; font-weight: 600; }}"
        for name, tone in t.tones.items()
    )
    return f"""
* {{ font-family: {FONT_STACK}; font-size: 10pt; }}
QMainWindow, QDialog, QWizard, QWidget#page {{ background: {t.window}; color: {t.text}; }}
QWizardPage {{ background: {t.window}; }}
QLabel {{ color: {t.text}; background: transparent; }}
QLabel[role="muted"] {{ color: {t.muted}; }}
QLabel[role="small"] {{ color: {t.muted}; font-size: 9pt; }}
QLabel[role="title"] {{ font-size: 18pt; font-weight: 600; }}
QLabel[role="heading"] {{ font-size: 11pt; font-weight: 600; }}
QLabel[role="section"] {{ color: {t.subtle}; font-size: 8.5pt; font-weight: 600; }}
QLabel[role="empty"] {{ color: {t.muted}; font-size: 11pt; }}
QLabel[role="path"] {{ font-family: {MONO_STACK}; font-size: 9pt; color: {t.text}; }}
QLabel[role="quote"] {{ background: {t.window}; color: {t.muted}; border-radius: 6px;
    padding: 8px 10px; }}
{tone_rules}
QLabel#banner {{ color: {t.banner.fg}; background: {t.banner.bg}; padding: 8px 20px;
    font-weight: 500; border-bottom: 1px solid {t.border}; }}
QLabel#brand {{ font-size: 12pt; font-weight: 600; }}
QFrame#card {{ background: {t.card}; border: 1px solid {t.border}; border-radius: 10px; }}
QFrame#inset {{ background: {t.window}; border: 1px solid {t.border}; border-radius: 8px; }}
QFrame#inset QLabel {{ background: transparent; }}
QFrame#divider {{ background: {t.border}; border: none; max-height: 1px; min-height: 1px; }}
QFrame#sidebar {{ background: {t.sidebar}; border: none; border-right: 1px solid {t.border}; }}
QFrame#updatecard {{ margin: 0 12px; background: {t.card}; border: 1px solid {t.border_strong};
    border-radius: 10px; }}
QListWidget#nav {{ background: transparent; border: none; outline: none; padding: 0; }}

QPushButton {{ background: {t.card}; color: {t.text}; border: 1px solid {t.border_strong};
    border-radius: 6px; padding: 6px 13px; font-weight: 500; }}
QPushButton:hover {{ background: {t.hover}; }}
QPushButton:pressed {{ background: {t.border}; }}
QPushButton:focus {{ border: 2px solid {t.accent}; padding: 5px 12px; }}
QPushButton:disabled {{ color: {t.subtle}; background: transparent; border-color: {t.border}; }}
QPushButton[primary="true"] {{ background: {t.accent}; color: {t.accent_text};
    border: 1px solid {t.accent}; }}
QPushButton[primary="true"]:hover {{ background: {t.accent_hover};
    border-color: {t.accent_hover}; }}
QPushButton[primary="true"]:focus {{ border: 2px solid {t.text}; padding: 5px 12px; }}
QPushButton[primary="true"]:disabled {{ background: {t.border}; color: {t.subtle};
    border-color: {t.border}; }}
QPushButton[danger="true"] {{ color: {t.tones["error"].fg}; }}
QPushButton[danger="true"]:hover {{ background: {t.tones["error"].bg}; }}
QPushButton:flat, QPushButton[ghost="true"] {{ background: transparent;
    border: 1px solid transparent; color: {t.muted}; }}
QPushButton:flat:hover, QPushButton[ghost="true"]:hover {{ background: {t.hover};
    color: {t.text}; }}
QPushButton:flat:focus, QPushButton[ghost="true"]:focus {{ border: 2px solid {t.accent};
    padding: 5px 12px; }}
QPushButton[ghost="true"][danger="true"] {{ color: {t.tones["error"].fg}; }}

QLineEdit, QPlainTextEdit, QTextEdit, QTextBrowser, QComboBox, QSpinBox, QTimeEdit {{
    background: {t.input_bg}; color: {t.text}; border: 1px solid {t.border_strong};
    border-radius: 6px; padding: 6px 10px; selection-background-color: {t.accent};
    selection-color: {t.accent_text}; }}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QTimeEdit:hover {{ border-color: {t.subtle}; }}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus, QTimeEdit:focus {{
    border: 2px solid {t.accent}; padding: 5px 9px; }}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{ color: {t.subtle};
    background: {t.window}; }}
QTextBrowser {{ background: {t.card}; border: 1px solid {t.border}; border-radius: 10px;
    padding: 14px 18px; }}
QComboBox {{ padding-right: 28px; min-height: 20px; }}
QComboBox::drop-down {{ border: none; width: 26px; subcontrol-origin: padding;
    subcontrol-position: center right; }}
QComboBox::down-arrow {{ {down} width: 14px; height: 14px; }}
QComboBox QAbstractItemView {{ background: {t.card}; color: {t.text};
    border: 1px solid {t.border_strong}; border-radius: 8px; padding: 4px; outline: none;
    selection-background-color: {t.accent_soft}; selection-color: {t.text}; }}
QComboBox QAbstractItemView::item {{ min-height: 26px; padding: 0 8px; border-radius: 4px; }}
QSpinBox, QTimeEdit {{ padding-right: 24px; }}
QSpinBox::up-button, QTimeEdit::up-button {{ subcontrol-origin: border;
    subcontrol-position: top right; width: 22px; border: none; background: transparent; }}
QSpinBox::down-button, QTimeEdit::down-button {{ subcontrol-origin: border;
    subcontrol-position: bottom right; width: 22px; border: none; background: transparent; }}
QSpinBox::up-arrow, QTimeEdit::up-arrow {{ {up} width: 12px; height: 12px; }}
QSpinBox::down-arrow, QTimeEdit::down-arrow {{ {down} width: 12px; height: 12px; }}

QPlainTextEdit#audit {{ background: {t.card}; border: 1px solid {t.border}; border-radius: 10px;
    padding: 12px 14px; font-family: {MONO_STACK}; font-size: 9pt; color: {t.muted}; }}
QListWidget, QTableWidget {{ background: {t.card}; color: {t.text}; border: 1px solid {t.border};
    border-radius: 10px; padding: 4px; outline: none;
    selection-background-color: {t.accent_soft}; selection-color: {t.text}; }}
QListWidget::item {{ padding: 6px 10px; border-radius: 6px; }}
QListWidget::item:hover {{ background: {t.hover}; }}
QListWidget::item:selected {{ background: {t.accent_soft}; color: {t.text}; }}
QTableWidget {{ gridline-color: transparent; padding: 0; }}
QTableWidget::item {{ padding: 4px 10px; border: none; border-bottom: 1px solid {t.border}; }}
QTableWidget::item:hover {{ background: {t.hover}; }}
QTableWidget::item:selected {{ background: {t.accent_soft}; color: {t.text}; }}
QHeaderView {{ background: transparent; }}
QHeaderView::section {{ background: {t.card}; color: {t.subtle}; border: none;
    border-bottom: 1px solid {t.border}; padding: 9px 10px; font-size: 8.5pt;
    font-weight: 600; }}
QTableCornerButton::section {{ background: {t.card}; border: none; }}

QCheckBox {{ spacing: 9px; color: {t.text}; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border: 1px solid {t.border_strong};
    border-radius: 4px; background: {t.input_bg}; }}
QCheckBox::indicator:hover {{ border-color: {t.accent}; }}
QCheckBox::indicator:checked {{ background: {t.accent}; border-color: {t.accent}; {check} }}
QCheckBox::indicator:disabled {{ border-color: {t.border}; background: {t.window}; }}

QProgressBar {{ background: {t.border}; border: none; border-radius: 4px; max-height: 8px;
    min-height: 8px; text-align: center; color: {t.text}; }}
QProgressBar::chunk {{ background: {t.accent}; border-radius: 4px; }}

QTabWidget::pane {{ border: none; border-top: 1px solid {t.border}; top: -1px; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{ padding: 9px 14px; margin-right: 6px; color: {t.muted}; background: transparent;
    border: none; border-bottom: 2px solid transparent; font-weight: 500; }}
QTabBar::tab:hover {{ color: {t.text}; }}
QTabBar::tab:selected {{ color: {t.text}; border-bottom: 2px solid {t.accent}; }}

QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 12px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {t.border_strong}; border-radius: 4px; min-height: 32px;
    margin: 0 2px; }}
QScrollBar::handle:vertical:hover {{ background: {t.subtle}; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {t.border_strong}; border-radius: 4px; min-width: 32px;
    margin: 2px 0; }}
QScrollBar::handle:horizontal:hover {{ background: {t.subtle}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none;
    background: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QStatusBar {{ background: {t.window}; color: {t.subtle}; border: none; font-size: 9pt; }}
QStatusBar::item {{ border: none; }}
QSplitter::handle {{ background: transparent; }}
QToolTip {{ background: {t.card}; color: {t.text}; border: 1px solid {t.border_strong};
    border-radius: 6px; padding: 5px 8px; }}
QMenu {{ background: {t.card}; color: {t.text}; border: 1px solid {t.border_strong};
    border-radius: 8px; padding: 5px; }}
QMenu::item {{ padding: 6px 22px 6px 12px; border-radius: 5px; }}
QMenu::item:selected {{ background: {t.accent_soft}; }}
QMenu::separator {{ height: 1px; background: {t.border}; margin: 5px 8px; }}
QGroupBox {{ border: 1px solid {t.border}; border-radius: 10px; margin-top: 14px; padding: 12px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; color: {t.muted}; }}
QMessageBox {{ background: {t.window}; }}
"""


def palette_for(t: Theme) -> QPalette:
    palette = QPalette()
    for role, color in (
        (QPalette.ColorRole.Window, t.window), (QPalette.ColorRole.WindowText, t.text),
        (QPalette.ColorRole.Base, t.input_bg), (QPalette.ColorRole.AlternateBase, t.window),
        (QPalette.ColorRole.Text, t.text), (QPalette.ColorRole.Button, t.card),
        (QPalette.ColorRole.ButtonText, t.text), (QPalette.ColorRole.Highlight, t.accent),
        (QPalette.ColorRole.HighlightedText, t.accent_text),
        (QPalette.ColorRole.PlaceholderText, t.subtle), (QPalette.ColorRole.ToolTipBase, t.card),
        (QPalette.ColorRole.ToolTipText, t.text),
        (QPalette.ColorRole.Link, t.link), (QPalette.ColorRole.LinkVisited, t.link),
    ):  # fmt: skip
        palette.setColor(role, QColor(color))
    return palette


def apply_theme(app: QApplication, theme: Theme | None = None) -> Theme:
    theme = theme or detect_theme(app)
    _shown[0] = theme
    if app.property(THEME_PROPERTY) == theme.name:
        return theme  # already showing it; restyling every widget again would only be slow
    if app.property(THEME_PROPERTY) is None:  # once: setting a style again under a style sheet
        app.setStyle(
            "Fusion"
        )  # can crash Qt. Fusion is a consistent base on every Windows 11 setup
    app.setPalette(palette_for(theme))  # so checkboxes, menus and dialogs follow the theme too
    app.setStyleSheet(stylesheet(theme, make_icons(theme)))
    app.setProperty(THEME_PROPERTY, theme.name)
    return theme

"""The look of the program: colours stay readable, and the custom-painted parts behave."""

from __future__ import annotations

from itertools import pairwise
from typing import Any

import pytest
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QWidget

from contentshare.ui.help_page import HelpPage, help_css
from contentshare.ui.nav import NavList, split_count
from contentshare.ui.theme import (
    DARK,
    LIGHT,
    Theme,
    apply_theme,
    current_theme,
    make_icons,
    stylesheet,
)
from contentshare.ui.widgets import page_header
from fake_gmail import make_message
from test_feedback_round import rig  # noqa: F401  (the window fixture)


def _ready_mail(harness: Any) -> None:
    harness.world.gmail.add(
        make_message(message_id="d1", subject="Sunrise Talk 12 Oct 2026", body="english")
    )
    harness.controller.check_now()


def _luminance(colour: str) -> float:
    c = QColor(colour)
    parts = []
    for v in (c.redF(), c.greenF(), c.blueF()):
        parts.append(v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4)
    return 0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2]


def ratio(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("theme", [LIGHT, DARK], ids=lambda t: t.name)
def test_every_text_and_background_pair_is_readable(theme: Theme) -> None:
    for surface in (theme.window, theme.card, theme.sidebar, theme.input_bg):
        assert ratio(theme.text, surface) >= 7, surface
        assert ratio(theme.muted, surface) >= 4.5, surface
        assert ratio(theme.subtle, surface) >= 4.5, surface
        assert ratio(theme.link, surface) >= 4.5, surface
    assert ratio(theme.text, theme.accent_soft) >= 7  # a selected row
    assert ratio(theme.text, theme.hover) >= 7
    assert ratio(theme.muted, theme.hover) >= 4.5
    assert ratio(theme.accent_text, theme.accent) >= 4.5  # the primary button
    assert ratio(theme.accent_text, theme.accent_hover) >= 3  # large enough to read while hovered
    assert ratio(theme.banner.fg, theme.banner.bg) >= 4.5
    for name, tone in theme.tones.items():
        assert ratio(tone.fg, tone.bg) >= 4.5, name
        assert ratio(tone.fg, theme.card) >= 4.5, name  # coloured words in the history table


@pytest.mark.parametrize("theme", [LIGHT, DARK], ids=lambda t: t.name)
def test_the_style_sheet_covers_the_widgets_and_uses_real_colours(theme: Theme, qapp: Any) -> None:
    sheet = stylesheet(theme, make_icons(theme))
    for selector in (
        "QPushButton",
        'QPushButton[primary="true"]',
        "QLineEdit",
        "QComboBox",
        "QTabBar::tab",
        "QScrollBar",
        "QTableWidget",
        "QFrame#card",
        "QFrame#updatecard",
        "QLabel#banner",
    ):
        assert selector in sheet, selector
    assert "{" in sheet and "{{" not in sheet and "}}" not in sheet  # no unfilled placeholders
    assert theme.accent in sheet and theme.card in sheet


def test_the_accent_is_the_same_in_both_themes() -> None:
    assert LIGHT.accent == DARK.accent == "#5E6AD2"


def test_the_icons_the_style_sheet_needs_exist(qapp: Any) -> None:
    icons = make_icons(DARK)
    for path in (icons.check, icons.down, icons.up):
        assert path.is_file() and path.stat().st_size > 0


def test_the_theme_that_is_on_screen_can_be_asked_for(qapp: Any) -> None:
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    apply_theme(app, DARK)
    assert current_theme() is DARK
    apply_theme(app, LIGHT)
    assert current_theme() is LIGHT


# --------------------------------------------------------------------------- navigation
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Requests (3)", ("Requests", 3)),
        ("Active copies (12)", ("Active copies", 12)),
        ("History", ("History", 0)),
        ("Odd (name) (7)", ("Odd (name)", 7)),
        ("Not a count (x)", ("Not a count (x)", 0)),
    ],
)
def test_the_count_is_split_from_the_screen_name(text: str, expected: tuple[str, int]) -> None:
    assert split_count(text) == expected


def test_the_navigation_list_paints_every_row_in_both_themes(qtbot: Any) -> None:
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    nav = NavList()
    qtbot.addWidget(nav)
    for name in ("Requests (3)", "Active copies", "History", "Settings", "Help (120)"):
        nav.addItem(name)
    nav.setCurrentRow(0)
    nav.resize(236, 260)
    for theme in (LIGHT, DARK):
        apply_theme(app, theme)
        nav.show()
        image = QImage(nav.size(), QImage.Format.Format_ARGB32)
        image.fill(QColor("white"))
        nav.render(image)
        # the selected row is tinted, so the picture is not one flat colour
        assert len({image.pixelColor(x, 20).rgb() for x in range(0, 236, 4)}) > 1
    assert nav.item(0).text() == "Requests (3)"  # the text stays plain for screen readers


# --------------------------------------------------------------------------- pages
def test_page_header_has_a_title_and_an_optional_line(qtbot: Any) -> None:
    block = page_header("Settings", "What this is for")
    texts = [block.itemAt(i).widget().text() for i in range(block.count())]
    assert texts == ["Settings", "What this is for"]
    assert page_header("Help").count() == 1


def test_every_screen_has_a_title_and_a_line_saying_what_it_is_for(rig: Any) -> None:  # noqa: F811
    from PySide6.QtWidgets import QLabel

    for page in (
        rig.window.requests,
        rig.window.copies,
        rig.window.history,
        rig.window.settings,
        rig.window.help,
    ):
        roles = [label.property("role") for label in page.findChildren(QLabel)]
        assert "title" in roles


def test_the_help_text_is_drawn_in_the_theme_colours(qtbot: Any) -> None:
    css = help_css(DARK)
    assert DARK.text in css and DARK.link in css and DARK.border in css
    page = HelpPage()
    qtbot.addWidget(page)
    html = page.browser.document().defaultStyleSheet()
    assert "font-size" in html


def test_status_words_in_the_history_table_take_their_tone(rig: Any) -> None:  # noqa: F811
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    apply_theme(app, DARK)
    _ready_mail(rig)
    page = rig.window.history
    page.refresh()
    status = page.table.item(0, 5)
    assert status is not None
    assert status.foreground().color().name().upper() == DARK.tones["ready"].fg.upper()


def test_an_empty_requests_screen_shows_a_message_and_hides_the_list(rig: Any) -> None:  # noqa: F811
    page = rig.window.requests
    assert page.empty.isVisible() and not page.scroller.isVisible()
    _ready_mail(rig)
    page.refresh()
    assert page.scroller.isVisible() and not page.empty.isVisible()


@pytest.mark.parametrize("width", [860, 1000, 1400])
def test_cards_never_overlap_clip_or_make_the_list_scroll_sideways(
    rig: Any,  # noqa: F811
    width: int,
) -> None:
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    for number, subject in enumerate(
        ("Sunrise Talk 12 Oct 2026", "Quantum Lecture", "Annual Day 2025 25 Dec 2025")
    ):
        rig.world.gmail.add(make_message(message_id=f"w{number}", subject=subject, body="english"))
    rig.controller.check_now()
    page = rig.window.requests
    page.refresh()
    rig.window.resize(width, 760)
    rig.window.show()
    app.processEvents()
    app.processEvents()
    cards = sorted(page._cards, key=lambda card: card.geometry().top())
    assert len(cards) >= 3
    for above, below in pairwise(cards):
        assert above.geometry().bottom() < below.geometry().top()
    for card in cards:
        for child in card.findChildren(QWidget):
            if child.isVisible() and child.parentWidget() is card:
                assert child.geometry().bottom() <= card.height(), (card.view.id, child)
                assert child.geometry().right() <= card.width(), (card.view.id, child)
    assert not page.scroller.horizontalScrollBar().isVisible()

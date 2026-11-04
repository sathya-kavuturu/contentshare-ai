"""Local times, the Appearance setting, the folder editor box, quick pickers, first screen."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication, QFileDialog, QLineEdit, QWizard

from contentshare.config.models import AppConfig
from contentshare.ui.folder_list import ROW_HEIGHT, FolderList
from contentshare.ui.theme import THEME_PROPERTY, apply_theme, theme_for
from contentshare.ui.timefmt import local_stamp
from contentshare.ui.wizard import FirstRunWizard
from test_folders_help import Rig, rig  # noqa: F401  (the window fixture)


def _app() -> QApplication:
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    return app


# --------------------------------------------------------------------------- times
def test_a_stored_utc_time_is_shown_on_the_local_clock() -> None:
    stored = "2026-11-03T12:51:48+00:00"
    expected = datetime(2026, 11, 3, 12, 51, 48, tzinfo=UTC).astimezone()
    assert local_stamp(stored) == expected.strftime("%Y-%m-%d %H:%M")
    assert local_stamp(stored, seconds=True) == expected.strftime("%Y-%m-%d %H:%M:%S")


def test_a_time_without_a_zone_is_read_as_utc() -> None:
    assert local_stamp("2026-11-03T12:51:48") == local_stamp("2026-11-03T12:51:48+00:00")


def test_a_plain_date_empty_text_and_odd_text_are_not_turned_into_something_else() -> None:
    assert local_stamp("2026-11-03") == "2026-11-03"
    assert local_stamp(None, empty="never") == "never"
    assert local_stamp("", empty="never") == "never"
    assert local_stamp("not a time") == "not a time"


def test_the_index_tab_shows_the_scan_time_on_the_local_clock(rig: Any) -> None:  # noqa: F811
    page = rig.window.settings
    stored = "2026-11-03T12:51:48+00:00"
    rig.controller.index_summary = lambda: {  # type: ignore[method-assign]
        "items": 5, "folders": 2, "last_scan": stored, "roots": 1,
    }  # fmt: skip
    page._refresh_status()
    assert local_stamp(stored) in page.index_state.text()


# --------------------------------------------------------------------------- appearance
def test_appearance_defaults_to_following_windows_and_old_files_still_load() -> None:
    assert AppConfig().appearance == "system"
    assert AppConfig.model_validate({"schema_version": 1}).appearance == "system"
    with pytest.raises(ValueError, match="appearance"):
        AppConfig.model_validate({"appearance": "purple"})


def test_theme_for_picks_the_named_theme() -> None:
    app = _app()
    assert theme_for("dark", app).name == "dark"
    assert theme_for("light", app).name == "light"
    assert theme_for("system", app).name in ("light", "dark")


def test_the_theme_is_not_applied_twice() -> None:
    app = _app()
    apply_theme(app, theme_for("dark", app))
    app.setStyleSheet("/* marker */")
    apply_theme(app, theme_for("dark", app))  # already dark: left alone
    assert app.styleSheet() == "/* marker */"
    apply_theme(app, theme_for("light", app))
    assert app.styleSheet() != "/* marker */" and app.property(THEME_PROPERTY) == "light"


def test_settings_offer_light_dark_and_same_as_windows(rig: Any) -> None:  # noqa: F811
    page = rig.window.settings
    shown = [page.appearance.itemText(i) for i in range(page.appearance.count())]
    assert shown == ["Same as Windows", "Light", "Dark"]
    assert page.collect().appearance == "system"
    page.appearance.setCurrentIndex(page.appearance.findData("dark"))
    assert page.collect().appearance == "dark"


def test_choosing_dark_shows_it_at_once_and_saving_keeps_it(rig: Any) -> None:  # noqa: F811
    page, app = rig.window.settings, _app()
    apply_theme(app, theme_for("light", app))
    page.appearance.setCurrentIndex(page.appearance.findData("dark"))
    page.appearance.activated.emit(page.appearance.currentIndex())
    assert app.property(THEME_PROPERTY) == "dark"
    assert page.save()
    assert rig.controller.config.appearance == "dark"
    page.appearance.setCurrentIndex(page.appearance.findData("light"))
    page.appearance.activated.emit(page.appearance.currentIndex())
    assert app.property(THEME_PROPERTY) == "light"
    page.load()  # Revert: back to the saved choice, on screen too
    assert page._appearance() == "dark" and app.property(THEME_PROPERTY) == "dark"


def test_help_follows_the_theme_change(rig: Any) -> None:  # noqa: F811
    app = _app()
    apply_theme(app, theme_for("light", app))
    rig.window.settings.appearance_changed.emit("dark")
    assert rig.window.help.browser.palette().link().color() == app.palette().link().color()


# --------------------------------------------------------------------------- folder editor
def test_the_path_editor_is_as_wide_as_the_row_and_tall_enough(qtbot: Any) -> None:
    widget = FolderList()
    qtbot.addWidget(widget)
    widget.resize(600, 300)
    widget.show()
    widget.set_paths([r"\\storage01\Programs\Library\Archive 2026\Talks"])
    widget.list.setCurrentRow(0)
    widget.edit_button.click()
    editor = widget.list.viewport().findChild(QLineEdit)
    assert editor is not None
    row = widget.list.visualItemRect(widget.list.item(0))
    assert row.height() >= ROW_HEIGHT
    assert editor.geometry().width() == row.width() and editor.height() >= ROW_HEIGHT


# --------------------------------------------------------------------------- pickers
def test_the_pickers_start_in_a_local_folder_not_where_windows_last_was(
    qtbot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[Any, ...]] = []

    def folder(*args: Any, **_kw: Any) -> str:
        seen.append(args)
        return "D:/Media"

    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(folder))
    widget = FolderList()
    qtbot.addWidget(widget)
    widget.browse.click()
    start = seen[0][2]
    assert start and not str(start).startswith("\\\\") and Path(start).is_dir()
    assert widget.paths() == ["D:\\Media"]


def test_the_settings_file_picker_starts_in_downloads(
    rig: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[Any, ...]] = []

    def pick(*args: Any, **_kw: Any) -> tuple[str, str]:
        seen.append(args)
        return ("C:/Users/x/Downloads/client_secret.json", "")

    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(pick))
    page = rig.window.settings
    page._browse_secret()
    assert page.secret.text() == "C:\\Users\\x\\Downloads\\client_secret.json"
    assert seen[0][2]  # a start folder was given


# --------------------------------------------------------------------------- first screen
def test_the_first_screen_has_no_back_button(qtbot: Any) -> None:
    wizard = FirstRunWizard(AppConfig())
    qtbot.addWidget(wizard)
    wizard.show()
    assert wizard.testOption(QWizard.WizardOption.NoBackButtonOnStartPage)
    assert not wizard.button(QWizard.WizardButton.BackButton).isVisible()
    wizard.next()
    assert wizard.button(QWizard.WizardButton.BackButton).isVisible()  # later pages can go back


def test_the_warm_up_shows_nothing_and_does_not_fail(rig: Any, qtbot: Any) -> None:  # noqa: F811
    rig.window.warm_up_pages()
    qtbot.wait(900)
    assert rig.window.stack.currentIndex() == 0


def test_changing_the_theme_many_times_with_a_screen_showing_does_not_crash(
    rig: Any,  # noqa: F811
) -> None:
    """Setting the Qt style again under a style sheet used to crash on the second change."""
    app = _app()
    rig.window.show_help("gmail")
    for name in ("dark", "light", "dark", "light", "dark", "system"):
        rig.window.settings.appearance_changed.emit(name)
        app.processEvents()
    assert rig.window.help.current_slug() == "gmail"

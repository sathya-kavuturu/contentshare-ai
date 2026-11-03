"""The editable folder list, the in-app Help, and how the window ties them together."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QApplication, QCheckBox, QFileDialog, QLabel, QPushButton

from contentshare.app.controller import Controller
from contentshare.app.updates import Updater
from contentshare.config.models import AppConfig
from contentshare.ui.bridge import Bridge
from contentshare.ui.folder_list import FolderList
from contentshare.ui.help_page import HELP_DIR, HelpPage, load_topics
from contentshare.ui.main_window import PAGES, MainWindow
from contentshare.ui.theme import apply_theme
from contentshare.ui.update_card import State
from contentshare.ui.wizard import FirstRunWizard
from helpers import TODAY, build_mail_world
from test_updates import FakeHttp, publish


@pytest.fixture(autouse=True)
def _theme() -> None:
    apply_theme(QApplication.instance())  # type: ignore[arg-type]


# --------------------------------------------------------------------------- folder list
@pytest.fixture
def folders(qtbot: Any) -> FolderList:
    widget = FolderList()
    qtbot.addWidget(widget)
    widget.show()
    return widget


def test_add_type_and_list(folders: FolderList) -> None:
    folders.typed.setText(r"  \\storage01\Programs  ")
    folders.add_typed()
    assert folders.paths() == [r"\\storage01\Programs"] and folders.typed.text() == ""
    folders.typed.setText("Z:\\Archive")
    folders.typed.returnPressed.emit()  # Enter in the box adds too
    assert folders.paths() == [r"\\storage01\Programs", "Z:\\Archive"]


def test_blank_input_adds_nothing(folders: FolderList) -> None:
    folders.typed.setText("   ")
    folders.add_typed()
    assert folders.paths() == [] and not folders.add("")


def test_the_same_folder_is_only_kept_once(folders: FolderList) -> None:
    assert folders.add("D:\\Programs")
    for same in ("d:\\programs", "D:\\Programs\\", "D:/Programs"):
        assert not folders.add(same)
    assert folders.paths() == ["D:\\Programs"] and "already in the list" in folders.note.text()
    folders.typed.setText("d:\\PROGRAMS")
    folders.add_typed()
    assert folders.typed.text() == "d:\\PROGRAMS"  # not cleared: it was not added


def test_remove_selected_takes_the_chosen_path_out(folders: FolderList) -> None:
    folders.set_paths(["A:\\one", "B:\\two", "C:\\three"])
    folders.list.setCurrentRow(1)
    folders.remove_button.click()
    assert folders.paths() == ["A:\\one", "C:\\three"]
    assert folders.list.currentRow() == 1  # the next one is selected, ready to remove again
    folders.remove_button.click()
    folders.remove_button.click()
    assert folders.paths() == []
    folders.remove_selected()  # nothing selected: harmless


def test_buttons_follow_the_selection(folders: FolderList) -> None:
    assert not folders.edit_button.isEnabled() and not folders.remove_button.isEnabled()
    folders.add("A:\\one")
    assert folders.edit_button.isEnabled() and folders.remove_button.isEnabled()
    folders.remove_selected()
    assert not folders.edit_button.isEnabled() and not folders.remove_button.isEnabled()


def test_the_delete_key_removes_the_selected_path(qtbot: Any, folders: FolderList) -> None:
    from PySide6.QtCore import Qt

    folders.set_paths(["A:\\one", "B:\\two"])
    folders.list.setCurrentRow(0)
    folders.list.setFocus()
    qtbot.waitUntil(folders.list.hasFocus)
    qtbot.keyClick(folders.list, Qt.Key.Key_Delete)
    assert folders.paths() == ["B:\\two"]


def test_edit_changes_a_path_in_place(folders: FolderList) -> None:
    folders.set_paths(["A:\\one", "B:\\two"])
    folders.list.setCurrentRow(0)
    folders.edit_button.click()  # opens the in-place editor
    folders.list.item(0).setText("  A:\\corrected  ")
    assert folders.paths() == ["A:\\corrected", "B:\\two"]


def test_editing_a_path_to_empty_or_to_a_duplicate_removes_it(folders: FolderList) -> None:
    folders.set_paths(["A:\\one", "B:\\two", "C:\\three"])
    folders.list.item(0).setText("")
    assert folders.paths() == ["B:\\two", "C:\\three"] and "empty path" in folders.note.text()
    folders.list.item(1).setText("b:\\TWO")
    assert folders.paths() == ["B:\\two"] and "already in the list" in folders.note.text()


def test_the_folder_picker_adds_with_windows_slashes(
    folders: FolderList, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: "D:/Media")
    )
    folders.browse.click()
    assert folders.paths() == ["D:\\Media"]
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: ""))
    folders.browse.click()  # cancelled
    assert folders.paths() == ["D:\\Media"]


def test_set_paths_replaces_and_skips_duplicates(folders: FolderList) -> None:
    folders.set_paths(["A:\\one", "a:\\ONE", "B:\\two"])
    assert folders.paths() == ["A:\\one", "B:\\two"]
    folders.set_paths([])
    assert folders.paths() == []


# --------------------------------------------------------------------------- wizard / settings
def test_the_wizard_can_remove_and_edit_storage_folders(qtbot: Any, tmp_path: Path) -> None:
    wizard = FirstRunWizard(AppConfig())
    qtbot.addWidget(wizard)
    wizard.typed.setText(str(tmp_path / "wrong"))
    wizard._add_typed()
    wizard.folders.add(str(tmp_path / "typo"))
    wizard.folders.list.setCurrentRow(0)
    wizard.folders.remove_button.click()  # the mistake from the report: it can now be removed
    wizard.folders.list.item(0).setText(str(tmp_path / "archive"))
    wizard.dept_name.setText("Media Team")
    wizard.dept_emails.setPlainText("anita@dept.org")
    wizard.dept_root.setText(str(tmp_path / "out"))
    assert wizard.build_config().source_roots == [str(tmp_path / "archive")]


def test_the_wizard_still_needs_one_storage_folder_after_removing_them_all(
    qtbot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown: list[str] = []
    from contentshare.ui import wizard as wizard_module

    monkeypatch.setattr(
        wizard_module.QMessageBox, "warning", staticmethod(lambda *a, **k: shown.append(a[2]))
    )
    wizard = FirstRunWizard(AppConfig())
    qtbot.addWidget(wizard)
    wizard.show()
    wizard.next()
    wizard.folders.add("Z:\\Programs")
    wizard.folders.remove_selected()
    wizard.next()
    assert wizard.currentPage() is wizard.storage_page and "storage folder" in shown[-1]


class Rig:
    def __init__(self, tmp_path: Path, db: Any, http: FakeHttp | None = None) -> None:
        world = build_mail_world(tmp_path, db)
        self.world, self.http = world, http or FakeHttp()
        self.bridge = Bridge()
        self.controller = Controller(
            world.repo, world.index, world.config, gateway_factory=lambda: world.gateway,
            config_path=tmp_path / "config.yaml", today=lambda: TODAY,
        )  # fmt: skip
        self.launched: list[Path] = []
        updater = Updater("0.1.5", tmp_path / "updates", self.http, launcher=self.launched.append)
        self.window = MainWindow(self.controller, self.bridge, updater=updater)


@pytest.fixture
def rig(qtbot: Any, tmp_path: Path, db: Any) -> Iterator[Rig]:
    r = Rig(tmp_path, db)
    qtbot.addWidget(r.window)
    r.window.show()
    yield r
    r.controller.shutdown()
    r.bridge.close()


def test_settings_remove_a_folder_and_save(rig: Rig) -> None:
    page = rig.window.settings
    start = list(rig.controller.config.source_roots)
    assert start, "the test world has a storage folder"
    page.folders.add("Z:\\Extra")
    page.folders.remove_button.click()  # removes the selected (just added) path
    assert page.collect().source_roots == start
    page.folders.list.setCurrentRow(0)
    page.folders.remove_button.click()
    assert page.collect().source_roots == start[1:]


def test_settings_save_the_update_option(rig: Rig) -> None:
    page = rig.window.settings
    assert page.check_updates.isChecked() and page.collect().updates.check_on_start
    page.check_updates.setChecked(False)
    assert not page.collect().updates.check_on_start


def test_an_old_settings_file_without_the_updates_section_still_loads() -> None:
    config = AppConfig.model_validate({"schema_version": 1, "source_roots": []})
    assert config.updates.check_on_start


# ---------------------------------------------------------------------------- update box
def test_the_box_sits_bottom_left_below_the_screens_list(rig: Rig) -> None:
    card = rig.window.update_card
    assert card is not None and card.isHidden()
    nav, side = rig.window.nav, card.parentWidget()
    assert side is not None
    layout = side.layout()
    assert layout is not None
    assert layout.indexOf(card) > layout.indexOf(nav)


def test_the_check_on_start_offers_an_update(
    qtbot: Any, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import contentshare.ui.main_window as module

    monkeypatch.setattr(module, "UPDATE_CHECK_DELAY_MS", 0)
    publish(rig.http, "0.2.0")
    rig.window.start_update_check()
    card = rig.window.update_card
    assert card is not None
    qtbot.waitUntil(lambda: card.state is State.AVAILABLE)
    assert "0.2.0" in rig.window.statusBar().currentMessage()


def test_the_check_on_start_can_be_turned_off(
    qtbot: Any, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import contentshare.ui.main_window as module

    monkeypatch.setattr(module, "UPDATE_CHECK_DELAY_MS", 0)
    config = rig.controller.config.model_copy(deep=True)
    config.updates.check_on_start = False
    rig.controller.apply_config(config, persist=False)
    publish(rig.http, "0.2.0")
    rig.window.start_update_check()
    qtbot.wait(300)
    assert rig.window.update_card is not None and rig.window.update_card.isHidden()


def test_check_for_updates_now_in_settings(qtbot: Any, rig: Rig) -> None:
    publish(rig.http, "0.1.5")
    rig.window.settings.update_now.click()
    card = rig.window.update_card
    assert card is not None
    qtbot.waitUntil(lambda: "latest version" in card.title.text())
    assert not card.isHidden()


def test_a_window_without_an_updater_has_no_box(qtbot: Any, tmp_path: Path, db: Any) -> None:
    world = build_mail_world(tmp_path, db)
    controller = Controller(
        world.repo, world.index, world.config, gateway_factory=lambda: world.gateway,
        config_path=tmp_path / "c.yaml", today=lambda: TODAY,
    )  # fmt: skip
    bridge = Bridge()
    window = MainWindow(controller, bridge)
    qtbot.addWidget(window)
    window.start_update_check()  # harmless
    window.check_for_updates()
    assert window.update_card is None
    controller.shutdown()
    bridge.close()


# --------------------------------------------------------------------------- help
def test_all_the_help_topics_are_bundled() -> None:
    topics = load_topics()
    assert [t.slug for t in topics] == ["start", "gmail", "use", "settings", "trouble"]
    assert [t.title for t in topics] == [
        "Getting started",
        "Connect Gmail",
        "Using the app",
        "Settings explained",
        "Troubleshooting",
    ]


def test_the_help_text_is_packaged_for_the_program() -> None:
    import tomllib

    root = Path(__file__).resolve().parents[1]
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert "help/*.md" in data["tool"]["setuptools"]["package-data"]["contentshare"]
    assert "contentshare/help" in (root / "packaging" / "build.py").read_text(encoding="utf-8")


def test_missing_help_files_do_not_crash(qtbot: Any, tmp_path: Path) -> None:
    assert load_topics(tmp_path / "nowhere") == []
    page = HelpPage(topics=[])
    qtbot.addWidget(page)
    assert "could not be found" in page.browser.toPlainText()
    (tmp_path / "notes.md").write_text("# ignored: not numbered")
    (tmp_path / "01-hello.md").write_text("# Hello\n\nworld", encoding="utf-8")
    assert [t.title for t in load_topics(tmp_path)] == ["Hello"]


def test_help_shows_each_topic(qtbot: Any) -> None:
    page = HelpPage()
    qtbot.addWidget(page)
    expected = {
        "start": "five steps",
        "gmail": "client_secret.json",
        "use": "Confirm copy",
        "settings": "Storage folders to search",
        "trouble": "Where things are kept",
    }
    for slug, words in expected.items():
        assert page.show_topic(slug) and page.current_slug() == slug
        assert words in page.browser.toPlainText(), slug
    assert not page.show_topic("nonsense")
    assert page.current_slug() == "trouble"


def test_every_help_link_points_somewhere_real() -> None:
    slugs = {t.slug for t in load_topics()}
    for topic in load_topics():
        for target in re.findall(r"\]\(help:([a-z]+)\)", topic.markdown):
            assert target in slugs, f"{topic.slug} links to missing topic {target}"
        for target in re.findall(r"\]\(([a-z]+:[^)]*)\)", topic.markdown):
            assert target.startswith(("help:", "https://")), target


def test_help_links_only_do_safe_things(qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from contentshare.ui import help_page

    opened: list[str] = []
    monkeypatch.setattr(
        help_page.QDesktopServices, "openUrl", staticmethod(lambda u: opened.append(u.toString()))
    )
    page = HelpPage()
    qtbot.addWidget(page)
    page._link(QUrl("help:gmail"))
    assert page.current_slug() == "gmail"
    page._link(QUrl("https://console.cloud.google.com"))
    for bad in (
        "http://example.org",
        "file:///C:/Windows/System32/calc.exe",
        "javascript:alert(1)",
    ):
        page._link(QUrl(bad))
    assert opened == ["https://console.cloud.google.com"]
    assert page.browser.loadResource(2, QUrl("https://example.org/x.png")) is None
    assert page.browser.loadResource(2, QUrl("file:///C:/x.png")) is None


def test_help_describes_every_setting_by_the_name_shown_on_screen(rig: Rig) -> None:
    """If a setting is renamed on the Settings screen, its help must be updated too."""
    settings = rig.window.settings
    help_text = {t.slug: t.markdown for t in load_topics()}["settings"]
    shown = [c.text() for c in settings.findChildren(QCheckBox)]
    assert len(shown) >= 8
    for text in shown:
        name = re.split(r": | \(| “", text)[0]
        assert name in help_text, f"Help does not mention the setting “{name}”"
    labels = {w.text() for w in settings.findChildren(QLabel)}  # includes the form's row captions
    for phrase in (
        "Daily check at", "Client file", "Look back", "Emails per check",
        "Mark Ready when at least", "Verification checksum", "Copies at the same time",
        "Attempts per file", "Default reply text", "Storage folders to search",
    ):  # fmt: skip
        assert phrase in help_text, phrase
        assert any(phrase in label for label in labels), f"{phrase} is no longer on screen"
    for tab in range(settings.tabs.count()):
        assert settings.tabs.tabText(tab) in help_text


def test_the_window_has_a_help_screen_and_can_open_a_topic(rig: Rig) -> None:
    window = rig.window
    assert PAGES[-1] == "Help" and window.nav.count() == len(PAGES)
    window.show_help("gmail")
    assert window.stack.currentWidget() is window.help
    assert window.nav.currentRow() == PAGES.index("Help")
    assert window.help.current_slug() == "gmail"
    window.show_help()  # no topic: stays where it was
    assert window.help.current_slug() == "gmail"


def test_settings_gmail_tab_links_to_the_gmail_help(rig: Rig) -> None:
    page = rig.window.settings
    button = next(b for b in page.findChildren(QPushButton) if b.text().startswith("How do I"))
    button.click()
    assert rig.window.stack.currentWidget() is rig.window.help
    assert rig.window.help.current_slug() == "gmail"


def test_the_help_folder_exists_where_the_program_looks() -> None:
    assert HELP_DIR.is_dir() and len(list(HELP_DIR.glob("*.md"))) == 5

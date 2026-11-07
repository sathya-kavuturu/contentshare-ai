"""The main window: navigation on the left, one screen on the right."""

from __future__ import annotations

import contextlib
from collections.abc import Callable

from PySide6.QtCore import QEasingCurve, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QCloseEvent, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from contentshare.app.controller import Controller, Event
from contentshare.app.notifications import Toast, for_check, for_event
from contentshare.app.scheduler import Due
from contentshare.app.startup import StartAtLogin
from contentshare.app.updates import Updater
from contentshare.gmail.intake import IntakeReport
from contentshare.ui.automation import Automation
from contentshare.ui.bridge import Bridge
from contentshare.ui.context import UiContext
from contentshare.ui.copies_page import CopiesPage
from contentshare.ui.developer_page import DeveloperPage
from contentshare.ui.dialogs import LoginDialog
from contentshare.ui.help_page import HelpPage
from contentshare.ui.history_page import HistoryPage
from contentshare.ui.icons import app_icon
from contentshare.ui.mailbox_page import MailboxPage
from contentshare.ui.nav import NavList
from contentshare.ui.requests_page import RequestsPage, summarize_check
from contentshare.ui.settings_page import SettingsPage
from contentshare.ui.theme import apply_theme, theme_for
from contentshare.ui.tray import Tray
from contentshare.ui.update_card import UpdateCard
from contentshare.ui.widgets import MENU_MS, BurgerButton
from contentshare.ui.widgets import Label as QLabel
from contentshare.ui.widgets import SafeMessageBox as QMessageBox

PAGES = ["Requests", "Active copies", "History", "Mailbox", "Settings", "Help", "Developer"]

SIDEBAR_WIDE, SIDEBAR_NARROW = 236, 68  # the menu on the left: full, or folded to icons
WARM_UP_DELAY_MS = 800
UPDATE_CHECK_DELAY_MS = 4000  # let the window appear and the first work settle before the check


def _banner(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("banner")
    label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    return label


def _brand() -> tuple[QWidget, QLabel]:
    """The program's icon and name at the top of the sidebar (and the name, to hide it)."""
    holder = QWidget()
    row = QHBoxLayout(holder)
    row.setContentsMargins(20, 8, 16, 14)
    row.setSpacing(10)
    mark = QLabel()
    mark.setPixmap(app_icon().pixmap(28, 28))
    row.addWidget(mark)
    name = QLabel("ContentShare")
    name.setObjectName("brand")
    row.addWidget(name, 1)
    return holder, name


class MainWindow(QMainWindow):
    def __init__(
        self,
        controller: Controller,
        bridge: Bridge,
        *,
        tray: Tray | None = None,
        automation: Automation | None = None,
        startup: StartAtLogin | None = None,
        updater: Updater | None = None,
        ask_login: bool = False,
    ) -> None:
        super().__init__()
        self.controller, self.bridge = controller, bridge
        self._ask_login_on_open = ask_login  # the real app asks who is using it; tests do not
        self.tray, self.automation = tray, automation
        self._quitting = False
        self._told_about_tray = False
        self.setWindowTitle("ContentShare")
        self.setWindowIcon(app_icon())
        self.setMinimumSize(920, 600)  # smaller than this the screens stop being usable
        self.resize(1180, 780)
        self.ctx = UiContext(controller, bridge, self.say, owner=self, startup=startup)

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.dry_banner = _banner("DRY-RUN MODE: approving only checks; nothing is copied or sent.")
        self.dev_banner = _banner(
            "DEVELOPER MODE is on: extra tracing is kept. Turn it off in Settings when done."
        )
        self.gmail_banner = _banner("Gmail is not connected. Open Settings → Gmail to connect.")
        outer.addWidget(self.dry_banner)
        outer.addWidget(self.dev_banner)
        outer.addWidget(self.gmail_banner)

        body = QHBoxLayout()
        body.setSpacing(0)
        self._build_sidebar(updater)
        body.addWidget(self.side)

        self.stack = QStackedWidget()
        self.requests = RequestsPage(self.ctx)
        self.copies = CopiesPage(self.ctx)
        self.history = HistoryPage(self.ctx)
        self.mailbox = MailboxPage(self.ctx)
        self.settings = SettingsPage(self.ctx)
        self.help = HelpPage()
        self.developer = DeveloperPage(self.ctx)
        for page in (
            self.requests, self.copies, self.history, self.mailbox, self.settings, self.help,
            self.developer,
        ):  # fmt: skip
            self.stack.addWidget(page)
        body.addWidget(self.stack, 1)
        outer.addLayout(body, 1)
        self.setCentralWidget(central)

        self.nav.currentRowChanged.connect(self._switch)
        self.nav.setCurrentRow(0)
        self.set_sidebar_collapsed(controller.config.sidebar_collapsed, remember=False)
        self._connect_services()
        self.statusBar().showMessage("Ready")
        self.update_chrome()
        self.nav.setFocus()  # not the menu icon: it would start with a ring around it

    # ------------------------------------------------------------------ the menu on the left
    def _build_sidebar(self, updater: Updater | None) -> None:
        self.side = QFrame()
        self.side.setObjectName("sidebar")
        side_layout = QVBoxLayout(self.side)
        side_layout.setContentsMargins(0, 0, 0, 14)
        side_layout.setSpacing(4)
        # the menu icon at the very top folds the menu to icons and opens it again
        self._slide = QVariantAnimation(self)
        self._slide.setDuration(MENU_MS)
        self._slide.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._slide.valueChanged.connect(lambda w: self.side.setFixedWidth(int(w)))
        self.top_toggle = BurgerButton()
        self.top_toggle.clicked.connect(lambda _c=False: self.toggle_sidebar())
        top_row = QHBoxLayout()
        top_row.setContentsMargins(16, 10, 12, 0)
        top_row.addWidget(self.top_toggle)
        top_row.addStretch(1)
        side_layout.addLayout(top_row)
        brand, self._brand_name = _brand()
        side_layout.addWidget(brand)
        self._brand_row = brand.layout()
        self.nav = NavList()
        self.nav.setAccessibleName("Screens")
        for name in PAGES:
            self.nav.addItem(QListWidgetItem(name))
        side_layout.addWidget(self.nav, 1)
        # who is using the app (click to change it): shown in the History beside approvals
        self.user_button = QPushButton()
        self.user_button.setFlat(True)
        self.user_button.clicked.connect(lambda _c=False: self.switch_user())
        user_row = QHBoxLayout()
        user_row.setContentsMargins(12, 0, 12, 0)
        user_row.addWidget(self.user_button)
        side_layout.addLayout(user_row)
        # Bottom left: "a new version is available" (only when an updater was given). It lives in
        # a holder so the whole box can be put away while the menu is folded.
        self._update_holder = QWidget()
        holder_layout = QVBoxLayout(self._update_holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        self.update_card = self._add_update_card(holder_layout, updater)
        side_layout.addWidget(self._update_holder)

    @property
    def sidebar_collapsed(self) -> bool:
        return self._sidebar_collapsed

    def toggle_sidebar(self) -> None:
        self.set_sidebar_collapsed(not self._sidebar_collapsed, animate=self.isVisible())

    def _stop_slide(self) -> None:
        """Stop a slide in progress without running what it was going to do when it ended."""
        with contextlib.suppress(RuntimeError, TypeError):
            self._slide.finished.disconnect()
        self._slide.stop()

    def _slide_sidebar(self, collapsed: bool, apply_content: Callable[[], None]) -> None:
        """Slide the menu to its new width. The names are put away when it has narrowed and
        shown again as it starts to widen, so they are never squeezed into a half-folded menu."""
        target = SIDEBAR_NARROW if collapsed else SIDEBAR_WIDE
        self._stop_slide()
        if not collapsed:
            apply_content()
        else:
            self._slide.finished.connect(apply_content)
        self._slide.setStartValue(self.side.width())
        self._slide.setEndValue(target)
        self._slide.start()

    def set_sidebar_collapsed(
        self, collapsed: bool, *, remember: bool = True, animate: bool = False
    ) -> None:
        """Fold the menu to icons, or show the names again. ``remember`` keeps the choice for the
        next start (not done when it is opened only to show an update). ``animate`` slides it."""
        self._sidebar_collapsed = collapsed

        def content() -> None:
            self._brand_name.setVisible(not collapsed)
            if self._brand_row is not None:
                margins = (20, 8, 20, 14) if collapsed else (20, 8, 16, 14)
                self._brand_row.setContentsMargins(*margins)
            self.nav.set_collapsed(collapsed)
            self._update_holder.setVisible(not collapsed)

        if animate:
            self._slide_sidebar(collapsed, content)
        else:
            self._stop_slide()
            self.side.setFixedWidth(SIDEBAR_NARROW if collapsed else SIDEBAR_WIDE)
            content()
        self.top_toggle.show_open(not collapsed, animate=animate)
        tip = "Show the menu names" if collapsed else "Fold the menu to icons"
        self.top_toggle.setToolTip(tip)
        self.top_toggle.setAccessibleName("Show the menu" if collapsed else "Fold the menu")
        self.refresh_user_button()
        if remember:
            with contextlib.suppress(OSError, ValueError):  # only "remember it" can fail
                self.controller.set_ui_option("sidebar_collapsed", collapsed)

    # ------------------------------------------------------------------ who is using the app
    def refresh_user_button(self) -> None:
        name = self.controller.user_name if self.controller.signed_in else ""
        self.user_button.setText("👤" if self._sidebar_collapsed else f"👤  {name or 'Sign in'}")
        tip = f"Signed in as {name}. Click to switch user." if name else "Sign in"
        self.user_button.setToolTip(tip)
        self.user_button.setAccessibleName(f"User: {name or 'not signed in'}. Switch user")

    def start_sign_in(self) -> bool:
        """The box asked each time the app is opened. Closing it without signing in quits."""
        self.controller.login_required = True
        dialog = LoginDialog(self.controller, self)
        signed_in = dialog.exec() == QDialog.DialogCode.Accepted
        self.refresh_user_button()
        if not signed_in:
            self.quit_app()
        return signed_in

    def switch_user(self) -> bool:
        """Sign in as someone else. Cancel keeps the person who is signed in."""
        dialog = LoginDialog(self.controller, self, can_cancel=True)
        switched = dialog.exec() == QDialog.DialogCode.Accepted
        self.refresh_user_button()
        return switched

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 (Qt name)
        super().showEvent(event)
        if self._ask_login_on_open:
            self._ask_login_on_open = False  # once per start, when the window first appears
            QTimer.singleShot(0, self.start_sign_in)

    def _add_update_card(self, layout: QVBoxLayout, updater: Updater | None) -> UpdateCard | None:
        if updater is None:
            return None
        card = UpdateCard(updater, self.bridge, self.controller.busy)
        layout.addWidget(card)
        return card

    def _connect_services(self) -> None:
        self.bridge.app_event.connect(self._on_event)
        self.settings.saved.connect(self._settings_saved)
        self.settings.appearance_changed.connect(self._appearance_changed)
        self.settings.open_help.connect(self.show_help)
        self.settings.update_check_requested.connect(self.check_for_updates)
        if self.update_card is not None:
            self.update_card.found.connect(self._update_found)
        if self.tray is not None:
            self.tray.open_requested.connect(self.show_window)
            self.tray.check_requested.connect(self.requests.check_now)
            self.tray.quit_requested.connect(self.quit_app)
        if self.automation is not None:
            self.automation.checked.connect(self._on_scheduled_check)

    # ------------------------------------------------------------------ showing / quitting
    def show_window(self, page: str | None = None) -> None:
        """Bring the window to the front (from the tray, a toast, or a second launch)."""
        if page in PAGES:
            self.nav.setCurrentRow(PAGES.index(page))
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def show_help(self, topic: str = "") -> None:
        """Open the Help screen, on ``topic`` (for example "gmail") when given."""
        self.show_window("Help")
        if topic:
            self.help.show_topic(topic)

    def warm_up_pages(self) -> None:
        """Lay out and paint the heavier screens once while the person is still reading the
        first one, so opening Settings or Help later is instant."""

        def warm() -> None:
            for page in (self.settings, self.help):
                page.grab()

        QTimer.singleShot(WARM_UP_DELAY_MS, warm)

    # ------------------------------------------------------------------ updates
    def start_update_check(self) -> None:
        """The check made when the program starts, if the user has not turned it off."""
        if self.update_card is not None and self.controller.config.updates.check_on_start:
            QTimer.singleShot(UPDATE_CHECK_DELAY_MS, lambda: self.check_for_updates(manual=False))

    def check_for_updates(self, manual: bool = True) -> None:
        if self.update_card is not None:
            if manual and self._sidebar_collapsed:
                self.set_sidebar_collapsed(False, remember=False)  # its answer appears there
            self.update_card.check(manual=manual)

    def _update_found(self, version: str) -> None:
        if self._sidebar_collapsed:
            self.set_sidebar_collapsed(False, remember=False)  # the box is in the menu
        self.say(f"ContentShare {version} is available. See the box at the bottom left.")
        self._toast(
            Toast(
                "A new version is available",
                f"ContentShare {version} can be downloaded. Open the window to update.",
                "info",
            )
        )

    def quit_app(self) -> None:
        """Really quit (the close button only hides the window when the tray is in use)."""
        self._quitting = True
        self.show_window()
        self.close()
        if self.isVisible():  # the user answered "No" to the running-copies question
            self._quitting = False

    def _keeps_running_in_tray(self) -> bool:
        return bool(
            not self._quitting
            and self.tray is not None
            and self.tray.visible()
            and self.controller.config.schedule.close_to_tray
        )

    # ------------------------------------------------------------------ settings, schedule
    def _appearance_changed(self, appearance: str) -> None:
        app = QApplication.instance()
        if isinstance(app, QApplication):
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)  # restyling takes a moment
            try:
                apply_theme(app, theme_for(appearance, app))  # nothing happens if already shown
                self.help.restyle()
            finally:
                QApplication.restoreOverrideCursor()

    def _settings_saved(self) -> None:
        if self.automation is not None:
            self.automation.config_changed()
        self.update_chrome()

    def _on_scheduled_check(self, report: IntakeReport | None, due: Due) -> None:
        if report is None:
            self.say("The scheduled check could not run. It will try again.")
        else:
            self.say(f"Scheduled check: {summarize_check(report)}")
        self.requests.refresh()
        self.update_chrome()
        if report is not None:
            self._toast(for_check(report, scheduled=True), force=report.needs_sign_in)

    def _toast(self, toast: Toast | None, *, force: bool = False) -> None:
        """Show a pop-up unless the user is already looking at the window."""
        if toast is None or self.tray is None or not self.controller.config.schedule.notifications:
            return
        if self.isActiveWindow() and not force:
            return
        self.tray.notify(toast)

    # ------------------------------------------------------------------ events
    def say(self, text: str) -> None:
        self.statusBar().showMessage(text, 15000)

    def _switch(self, row: int) -> None:
        if row < 0:
            return
        settings_row = PAGES.index("Settings")
        if (
            self.stack.currentIndex() == settings_row
            and row != settings_row
            and not self.settings.settle_unsaved()
        ):
            self.nav.blockSignals(True)  # stay in Settings, and show the menu that way again
            self.nav.setCurrentRow(settings_row)
            self.nav.blockSignals(False)
            return
        self.stack.setCurrentIndex(row)
        if PAGES[row] == "Active copies":
            self.copies.refresh()
        elif PAGES[row] == "History":
            self.history.refresh()
        elif PAGES[row] == "Mailbox":
            self.mailbox.refresh()
        elif PAGES[row] == "Settings":
            self.settings.show_page()

    def _on_event(self, event: Event) -> None:
        self.requests.on_event(event)
        self.copies.on_event(event)
        self.history.on_event(event)
        self.mailbox.on_event(event)
        if event.kind == "copy_finished" or event.kind in ("reply_sent", "reply_problem"):
            self.say(f"Request #{event.request_id}: {event.message}")
            self._toast(for_event(event))
        if event.kind == "telegram_problem":
            self.say(f"Telegram alert not sent: {event.message}")
        if event.kind in ("changed", "copy_finished"):
            self.update_chrome()

    def _show_developer(self, on: bool) -> None:
        """The Developer screen is in the menu only while developer mode is on."""
        row = PAGES.index("Developer")
        item = self.nav.item(row)
        if item is not None and item.isHidden() == on:
            item.setHidden(not on)
        self.dev_banner.setVisible(on)
        if not on and self.stack.currentIndex() == row:
            self.nav.setCurrentRow(PAGES.index("Settings"))

    def update_chrome(self) -> None:
        review = self.controller.attention_count()  # emails, not folders
        self.nav.item(0).setText(f"Requests ({review})" if review else "Requests")
        active = len(self.controller.queue.active())
        self.nav.item(1).setText(f"Active copies ({active})" if active else "Active copies")
        self.nav.refresh_tooltips()
        self._show_developer(self.controller.config.dev.enabled)
        self.dry_banner.setVisible(self.controller.dry_run)
        self.gmail_banner.setVisible(not self.controller.gmail_connected())
        if self.tray is not None:
            self.tray.set_status(self._schedule_text(), waiting=review)

    def _schedule_text(self) -> str:
        if self.automation is None:
            return ""
        nxt = self.automation.next_run()
        return f"Next check {nxt.astimezone():%a %d %b, %H:%M}" if nxt else "Automatic check is off"

    # ------------------------------------------------------------------ closing
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        if self._keeps_running_in_tray():
            event.ignore()
            self.hide()
            if self.tray is not None and not self._told_about_tray:
                self._told_about_tray = True
                self.tray.notify(
                    Toast(
                        "ContentShare is still running",
                        "It stays in the notification area to run the daily check. "
                        "Use the tray icon's menu to quit.",
                        "info",
                    )
                )
            return
        if self.controller.busy():
            answer = QMessageBox.question(
                self,
                "Copies are running",
                "Copies are still running. If you quit now they stop and continue from where "
                "they left off the next time ContentShare starts. Quit anyway?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self.cleanup()
        event.accept()
        QApplication.quit()  # the app stays alive with only a tray icon, so ask explicitly

    def cleanup(self) -> None:
        """Stop background work. Safe to call twice (window close and application exit)."""
        if self.automation is not None:
            self.automation.stop()
        if self.tray is not None:
            self.tray.hide()
        self.controller.shutdown()
        self.bridge.close()

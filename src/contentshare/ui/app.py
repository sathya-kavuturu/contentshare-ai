"""Starting the application: wiring, first-run setup, recovery after a crash."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtWidgets import QApplication

from contentshare import __version__
from contentshare.app.controller import Controller
from contentshare.app.startup import BACKGROUND_FLAG, StartAtLogin
from contentshare.config.store import (
    ConfigError,
    data_dir,
    default_config_path,
    load_config,
    save_config,
)
from contentshare.gmail.auth import GmailAuth
from contentshare.gmail.client import build_gateway
from contentshare.index.store import ContentIndex
from contentshare.logsafe import SafeFormatter
from contentshare.selftest import selftest
from contentshare.storage.db import Database
from contentshare.storage.repository import Repository
from contentshare.ui.automation import Automation
from contentshare.ui.bridge import Bridge
from contentshare.ui.icons import app_icon
from contentshare.ui.main_window import MainWindow
from contentshare.ui.single_instance import SingleInstance
from contentshare.ui.theme import apply_theme
from contentshare.ui.tray import Tray
from contentshare.ui.widgets import SafeMessageBox as QMessageBox
from contentshare.ui.wizard import FirstRunWizard

log = logging.getLogger("contentshare")

APP_USER_MODEL_ID = "ContentShare.App"  # the installer's Start-menu shortcut carries the same id


def _say(text: str) -> None:
    if sys.stdout is not None:  # a windowed program has no console
        print(text)


def setup_logging(folder: Path) -> Path:
    """A rotating log file. Tokens and email text are never logged by the application."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "contentshare.log"
    handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(SafeFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    return path


def build_controller(config_path: Path | None = None) -> Controller:
    home = data_dir()
    config_path = config_path or default_config_path()
    config = load_config(config_path)
    db = Database(home / "contentshare.db")
    repo = Repository(db)
    index = ContentIndex(db, config.index)
    auth = GmailAuth()
    holder: list[Controller] = []

    def gateway():  # type: ignore[no-untyped-def]
        return build_gateway(auth, holder[0].config)  # always the current settings

    controller = Controller(
        repo, index, config, gateway_factory=gateway, auth=auth, config_path=config_path
    )
    holder.append(controller)
    return controller


def needs_first_run(controller: Controller) -> bool:
    return not controller.config.departments or not controller.config.source_roots


def parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(prog="contentshare", add_help=False)
    parser.add_argument(
        BACKGROUND_FLAG, action="store_true",
        help="start hidden in the tray (used when Windows starts the app at sign-in)",
    )  # fmt: skip
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    parser.add_argument(
        "--selftest", nargs="?", const="", default=None, metavar="REPORT",
        help="check every part of the program, then exit (optionally write a JSON report)",
    )  # fmt: skip
    parser.add_argument(
        "--quit", action="store_true",
        help="ask a running ContentShare to quit and exit (used by the installer)",
    )  # fmt: skip
    parser.add_argument(
        "--remove-startup", action="store_true",
        help="remove the start-with-Windows entry and exit (used by the uninstaller)",
    )  # fmt: skip
    return parser.parse_known_args(argv)


def set_app_identity() -> None:
    """Tell Windows which application this is, so pop-ups, the taskbar and the Start-menu
    shortcut created by the installer all belong to one "ContentShare"."""
    with contextlib.suppress(AttributeError, OSError):
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)


def run(argv: list[str] | None = None) -> int:
    args, qt_args = parse_args(list(sys.argv[1:] if argv is None else argv[1:]))
    if args.version:
        _say(f"ContentShare {__version__}")
        return 0
    if args.quit:
        return 0 if SingleInstance(data_dir()).ask_to_quit() else 1
    if args.remove_startup:
        StartAtLogin().disable()
        return 0
    if args.selftest is not None:
        return selftest(args.selftest or None)
    set_app_identity()
    app = QApplication([sys.argv[0] if argv is None else argv[0], *qt_args])
    app.setApplicationName("ContentShare")
    app.setQuitOnLastWindowClosed(False)  # the tray keeps it alive; quitting is explicit
    app.setWindowIcon(app_icon())
    apply_theme(app)

    instance = SingleInstance(data_dir())
    if not instance.acquire():
        return 0  # the running copy was asked to show itself
    try:
        return _run(app, instance, background=args.background)
    finally:
        instance.release()


def _run(app: QApplication, instance: SingleInstance, *, background: bool) -> int:
    try:
        log_path = setup_logging(data_dir() / "logs")
        controller = build_controller()
    except (ConfigError, RuntimeError, OSError) as exc:
        QMessageBox.critical(
            None, "ContentShare cannot start",
            f"{exc}\n\nFix the problem (the settings file is {default_config_path()}) "
            "and start again.",
        )  # fmt: skip
        return 1
    log.info("started; log file %s", log_path)

    bridge = Bridge()
    controller.subscribe(bridge.forward)

    first_run = needs_first_run(controller)
    if first_run:
        wizard = FirstRunWizard(controller.config)
        if wizard.exec() and wizard.result_config is not None:
            save_config(wizard.result_config, default_config_path())
            controller.apply_config(wizard.result_config, persist=False)

    tray = Tray() if Tray.available() else None
    if tray is not None:
        tray.show()
    else:
        log.info("no notification area available; running without a tray icon")
    automation = Automation(controller, bridge)
    startup = StartAtLogin()
    window = MainWindow(controller, bridge, tray=tray, automation=automation, startup=startup)
    instance.show_requested.connect(window.show_window)
    instance.quit_requested.connect(window.quit_app)
    app.aboutToQuit.connect(window.cleanup)  # also runs when Windows signs the user out

    # Opened by Windows at sign-in: stay in the tray and let the daily check do its work.
    if not (background and tray is not None and not first_run):
        window.show()
    automation.start()
    window.update_chrome()

    # After a crash or restart: finish what the user had already approved, and settle replies
    # that were half-sent. Nothing new is approved or sent here.
    def recover() -> None:
        controller.recover_replies()
        controller.resume_approved()
        # Keep the Windows start-up entry pointing at this installation (it breaks if the
        # program folder is moved). Never creates one the user did not ask for.
        if controller.config.schedule.start_with_windows:
            try:
                startup.apply(True)
            except OSError as exc:
                log.warning("could not refresh the start-up entry: %s", exc)

    bridge.work(recover)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(run())

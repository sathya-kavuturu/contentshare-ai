"""Make the pictures used in docs/USER_GUIDE.md.

Everything shown is invented sample data in a temporary folder: no mailbox, no storage server and
no real folder is touched. Run it from the project folder (needs the development environment):

    python tools/make_screenshots.py            # writes docs/screenshots/*.png

Use a real Windows display for the real fonts:  set QT_QPA_PLATFORM=windows
"""

from __future__ import annotations

import shutil
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
OUT = ROOT / "docs" / "screenshots"

from PySide6.QtWidgets import QApplication  # noqa: E402

from contentshare.app.controller import Controller  # noqa: E402
from contentshare.copy_engine.models import Progress  # noqa: E402
from contentshare.storage.db import Database  # noqa: E402
from contentshare.ui.bridge import Bridge  # noqa: E402
from contentshare.ui.dialogs import LoginDialog  # noqa: E402
from contentshare.ui.main_window import MainWindow  # noqa: E402
from contentshare.ui.theme import apply_theme, theme_for  # noqa: E402
from fake_gmail import make_message  # noqa: E402
from helpers import TODAY, build_mail_world  # noqa: E402

WEEK = (
    "Amber Step 4 English 31-oct-2026",
    "Amber 7 Days Hindi 02-nov-2026",
    "Velvet Comet Open Floor 3rd Week 01-nov-2026",
)


def pump(app: QApplication, rounds: int = 5) -> None:
    for _ in range(rounds):
        app.processEvents()


def main() -> None:  # noqa: PLR0915  (a script that takes the pictures in order)
    OUT.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    apply_theme(app, theme_for("light", app))
    # a short, plain folder name so the paths in the pictures read well (removed again at the end)
    tmp = Path("C:/Users/Public/ContentShareDemo")
    if tmp.exists():
        raise SystemExit(f"{tmp} exists already; remove it first")
    tmp.mkdir()
    db = Database(tmp / "t.db")
    world = build_mail_world(tmp, db)
    for name in WEEK[:2]:  # the third one is on purpose not on the server
        folder = world.storage / "Weekly" / name
        folder.mkdir(parents=True)
        (folder / "talk.mp4").write_bytes(b"x" * 4000)
    world.index.scan_all([str(world.storage)])
    config = world.config.model_copy(
        update={
            "dry_run": False,
            "departments": [
                d.model_copy(update={"exact_folder_names": True}) for d in world.config.departments
            ],
        }
    )
    bridge = Bridge()
    ctl = Controller(
        world.repo, world.index, config, gateway_factory=lambda: world.gateway,
        config_path=tmp / "config.yaml", today=lambda: TODAY,
    )  # fmt: skip
    ctl.subscribe(bridge.forward)
    ctl.create_user("Ravi", "demo")  # invented users, so the pictures show names
    ctl.create_user("Asha", "demo")
    win = MainWindow(ctl, bridge)
    win.resize(1180, 860)
    win.gmail_banner.setMaximumHeight(0)  # keep it out of the pictures
    win.gmail_banner.hide()  # the fake mail box is not a real sign-in; keep the pictures clean
    win.show()

    def shot(widget: object, name: str) -> None:
        win.gmail_banner.hide()
        pump(app)
        widget.grab().save(str(OUT / name))  # type: ignore[attr-defined]
        print("wrote", name)

    # One finished copy first, so History has something to show.
    world.gmail.add(
        make_message(message_id="d1", subject="Content request", body="2026-10-12_SunriseTalk_EN")
    )
    ctl.check_now()
    first = ctl.views()[0]
    ctl.approve(first.id, confirmed=True)
    while ctl.busy():
        pump(app)
        threading.Event().wait(0.05)

    # The request email: a list of three names, one not on the server, and a stray line.
    body = (
        "Hello,\nPlease share these for the week:\n"
        + "\n".join(f"Nov 22-28  {n}" for n in WEEK)
        + "\nAlso need the Silver Bay recording from Sunday\nThanks"
    )
    world.gmail.add(make_message(message_id="d2", subject="Content for next week", body=body))
    ctl.check_now()
    win.requests.refresh()
    win.nav.setCurrentRow(0)
    win.resize(1180, 1560)
    shot(win, "requests-light.png")
    bar = win.requests.scroller.verticalScrollBar()
    bar.setValue(bar.maximum())
    shot(win, "requests-missing.png")  # the end of the same email: the folder that was not found
    bar.setValue(0)
    win.resize(1180, 860)

    # Folded menu
    win.top_toggle.click()
    shot(win, "menu-folded.png")
    win.top_toggle.click()

    # The sign-in box (two invented users)
    box = LoginDialog(ctl)
    box.show()
    pump(app)
    box.grab().save(str(OUT / "login.png"))
    print("wrote login.png")
    box.close()

    # Active copies: hold a copy half way so there is progress to show.
    world.gmail.add(
        make_message(
            message_id="d3", subject="Content request", body="YouthRetreat_2026-09-04_Day2"
        )
    )
    ctl.check_now()
    gate = threading.Event()
    runner = ctl.queue._runner
    real_run = runner.run

    def slow(request_id: int, control: object, **kw: object) -> object:
        gb = 1024**3
        kw["progress"](  # type: ignore[operator]
            Progress("talk.mp4", "verify", 0, 0, int(44.9 * gb), int(11.3 * gb), int(44.9 * gb),
                     202.2 * 1024**2, 112.0)
        )  # fmt: skip
        gate.wait(20)
        return real_run(request_id, control, **kw)

    runner.run = slow  # type: ignore[method-assign]
    ready = [v for v in ctl.views() if v.state == "READY"]
    if ready:
        ctl.approve(ready[0].id, confirmed=True)
        win.nav.setCurrentRow(1)
        win.copies.refresh()
        shot(win, "active-copies.png")
    gate.set()
    while ctl.busy():
        pump(app)
        threading.Event().wait(0.05)

    win.nav.setCurrentRow(2)
    win.history.refresh()
    shot(win, "history.png")

    win.nav.setCurrentRow(3)
    for index, name in ((0, "settings-general.png"), (1, "settings-departments.png")):
        win.settings.tabs.setCurrentIndex(index)
        shot(win, name)

    win.nav.setCurrentRow(4)
    shot(win, "help.png")

    # Dark
    apply_theme(app, theme_for("dark", app))
    win.nav.setCurrentRow(0)
    win.requests.refresh()
    shot(win, "requests-dark.png")
    ctl.shutdown()
    bridge.close()
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()

"""Phase 6: daily schedule, start at login, notifications, tray, single instance."""

from __future__ import annotations

import contextlib
import subprocess
import uuid
import winreg
from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication

from contentshare.app import startup as startup_module
from contentshare.app.controller import COPY_OK, Controller, Event
from contentshare.app.notifications import Toast, for_check, for_event
from contentshare.app.scheduler import (
    RETRY_DELAYS,
    STARTUP_DELAY,
    DailyScheduler,
    latest_occurrence,
    next_occurrence,
    parse_time,
)
from contentshare.app.startup import (
    BACKGROUND_FLAG,
    SchTasks,
    StartAtLogin,
    WindowsRegistryRun,
    launch_command,
)
from contentshare.gmail.errors import GmailError
from contentshare.gmail.intake import IntakeReport
from contentshare.storage.db import Database
from contentshare.ui.automation import Automation
from contentshare.ui.bridge import Bridge
from contentshare.ui.icons import app_icon
from contentshare.ui.main_window import MainWindow
from contentshare.ui.single_instance import SingleInstance
from contentshare.ui.theme import apply_theme
from contentshare.ui.tray import Tray
from contentshare.ui.widgets import SafeMessageBox as QMessageBox
from fake_gmail import make_message
from helpers import TODAY, MailWorld, build_mail_world


class Clock:
    def __init__(self, start: datetime) -> None:
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kwargs: float) -> None:
        self.t += timedelta(**kwargs)


def at(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, second, tzinfo=UTC)


# --------------------------------------------------------------------------- scheduler
def test_parse_time_and_occurrences() -> None:
    assert parse_time("07:05") == time(7, 5)
    assert latest_occurrence(at(3, 10), time(9, 0)) == at(3, 9)
    assert latest_occurrence(at(3, 8), time(9, 0)) == at(2, 9)
    assert next_occurrence(at(3, 8), time(9, 0)) == at(3, 9)
    assert next_occurrence(at(3, 9), time(9, 0)) == at(4, 9)  # strictly after


def make_scheduler(now: datetime) -> tuple[DailyScheduler, Clock]:
    clock = Clock(now)
    return DailyScheduler(now=clock), clock


def test_never_checked_runs_once_shortly_after_start() -> None:
    sched, clock = make_scheduler(at(3, 10))
    sched.start("09:00", last_check=None, run_missed=True)
    assert sched.poll() is None  # the network may not be up yet at sign-in
    clock.advance(seconds=STARTUP_DELAY.total_seconds() + 1)
    due = sched.poll()
    assert due is not None
    assert due.reason == "missed"
    assert sched.poll() is None  # one check at a time


def test_missed_check_is_detected_from_the_last_check_time() -> None:
    # Yesterday's check ran, today's 09:00 passed while the app was closed.
    sched, clock = make_scheduler(at(3, 10))
    sched.start("09:00", last_check=at(2, 9, 1), run_missed=True)
    assert sched.poll() is None  # not before the start-up delay
    clock.advance(seconds=STARTUP_DELAY.total_seconds() + 1)
    due = sched.poll()
    assert due is not None
    assert due.reason == "missed"


def test_check_already_done_today_waits_for_tomorrow() -> None:
    sched, clock = make_scheduler(at(3, 10))
    sched.start("09:00", last_check=at(3, 9, 5), run_missed=True)
    assert sched.next_run == at(4, 9)
    clock.advance(hours=22)
    assert sched.poll() is None
    clock.advance(hours=2)
    due = sched.poll()
    assert due is not None
    assert due.reason == "scheduled"


def test_missed_check_ignored_when_the_setting_is_off() -> None:
    sched, clock = make_scheduler(at(3, 10))
    sched.start("09:00", last_check=None, run_missed=False)
    clock.advance(minutes=5)
    assert sched.poll() is None
    assert sched.next_run == at(4, 9)


def test_sleeping_through_the_check_time_runs_it_once_on_wake() -> None:
    sched, clock = make_scheduler(at(3, 8))
    sched.start("09:00", last_check=at(2, 9), run_missed=False)
    clock.t = at(3, 15)  # the laptop slept from 08:30 to 15:00
    assert sched.poll() is not None
    sched.finished(ok=True)
    assert sched.poll() is None  # not again at 15:00:15
    assert sched.next_run == at(4, 9)


def test_failed_checks_retry_with_backoff_then_give_up_until_tomorrow() -> None:
    sched, clock = make_scheduler(at(3, 8, 59, 59))
    sched.start("09:00", last_check=at(2, 9), run_missed=False)
    clock.t = at(3, 9, 0, 1)
    for delay in RETRY_DELAYS:
        due = sched.poll()
        assert due is not None
        nxt = sched.finished(ok=False)
        assert nxt == clock.t + delay
        clock.t = nxt
    due = sched.poll()
    assert due is not None
    assert due.reason == "retry"
    assert sched.finished(ok=False) == next_occurrence(clock.t, time(9, 0))
    assert sched.poll() is None


def test_sign_in_problem_is_not_retried() -> None:
    sched, clock = make_scheduler(at(3, 8, 59, 59))
    sched.start("09:00", last_check=at(2, 9), run_missed=False)
    clock.t = at(3, 9, 0, 1)
    assert sched.poll() is not None
    assert sched.finished(ok=False, needs_sign_in=True) == at(4, 9)


def test_changing_the_time_never_triggers_a_check_by_itself() -> None:
    sched, clock = make_scheduler(at(3, 10))
    sched.start("09:00", last_check=at(3, 9, 1), run_missed=True)
    sched.reconfigure("10:30")
    assert sched.next_run == at(3, 10, 30)
    sched.reconfigure("08:00")  # earlier than now: tomorrow, not an immediate run
    assert sched.next_run == at(4, 8)
    assert sched.poll() is None


def test_disabled_scheduler_stays_quiet() -> None:
    sched, clock = make_scheduler(at(3, 10))
    sched.start("09:00", last_check=None, run_missed=True, enabled=False)
    clock.advance(days=3)
    assert sched.poll() is None
    assert sched.next_run is None


# --------------------------------------------------------------------------- start at login
def test_launch_command_is_quoted_and_marked_background(tmp_path: Path) -> None:
    exe = tmp_path / "Program Files" / "ContentShare.exe"
    frozen = launch_command(frozen=True, executable=str(exe))
    assert frozen == f'"{exe}" {BACKGROUND_FLAG}'
    python = tmp_path / "venv dir" / "python.exe"
    python.parent.mkdir()
    python.write_text("")
    source = launch_command(frozen=False, executable=str(python))
    assert source == f'"{python}" -m contentshare {BACKGROUND_FLAG}'  # no pythonw next to it
    (python.parent / "pythonw.exe").write_text("")
    assert launch_command(frozen=False, executable=str(python)).startswith(
        f'"{python.parent / "pythonw.exe"}"'
    )


class FakeRegistry:
    def __init__(self, value: str | None = None, writable: bool = True) -> None:
        self.value, self.writable = value, writable

    def get(self) -> str | None:
        return self.value

    def set(self, command: str) -> None:
        if not self.writable:
            raise PermissionError("blocked by policy")
        self.value = command

    def remove(self) -> None:
        self.value = None


class FakeTasks:
    def __init__(self, present: bool = False) -> None:
        self.present, self.command = present, None

    def exists(self) -> bool:
        return self.present

    def create(self, command: str) -> None:
        self.present, self.command = True, command

    def delete(self) -> None:
        self.present = False


def test_enable_uses_the_registry_and_removes_any_task() -> None:
    reg, tasks = FakeRegistry(), FakeTasks(present=True)
    login = StartAtLogin(reg, tasks, command="cmd-a")
    assert login.enable() == "registry"
    assert reg.value == "cmd-a"
    assert not tasks.present  # never both: the app would start twice
    status = login.status()
    assert (status.enabled, status.method, status.current) == (True, "registry", True)


def test_enable_falls_back_to_task_scheduler_when_the_registry_is_blocked() -> None:
    reg, tasks = FakeRegistry(writable=False), FakeTasks()
    login = StartAtLogin(reg, tasks, command="cmd-a")
    assert login.enable() == "task"
    assert tasks.command == "cmd-a"
    assert login.status().method == "task"


def test_apply_refreshes_a_stale_command_and_disables_cleanly() -> None:
    reg, tasks = FakeRegistry("old-location"), FakeTasks()
    login = StartAtLogin(reg, tasks, command="new-location")
    assert not login.status().current
    assert login.apply(True).current
    assert reg.value == "new-location"
    assert not login.apply(False).enabled
    assert reg.value is None
    assert not login.apply(False).enabled  # idempotent


def test_apply_false_does_not_touch_anything_when_disabled() -> None:
    reg, tasks = FakeRegistry(), FakeTasks()
    StartAtLogin(reg, tasks, command="x").apply(False)
    assert reg.value is None
    assert not tasks.present


def test_registry_backend_round_trip_in_a_throwaway_key() -> None:
    key = rf"Software\ContentShareTest-{uuid.uuid4().hex}"
    backend = WindowsRegistryRun("ContentShare", key=key)
    try:
        assert backend.get() is None
        backend.set('"C:\\Some App\\app.exe" --background')
        assert backend.get() == '"C:\\Some App\\app.exe" --background'
        backend.remove()
        assert backend.get() is None
        backend.remove()  # removing twice is fine
    finally:
        with contextlib.suppress(FileNotFoundError):
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)


def test_schtasks_uses_fixed_arguments_and_reports_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    code = {"value": 0}

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        assert kwargs.get("shell") is not True
        return subprocess.CompletedProcess(args, code["value"], "", "Access is denied")

    monkeypatch.setattr(startup_module.subprocess, "run", fake_run)
    tasks = SchTasks("Test task")
    tasks.create('"C:\\x y\\a.exe" --background')
    create = calls[-1]
    assert create[0].lower().endswith(r"system32\schtasks.exe")
    assert create[create.index("/TR") + 1] == '"C:\\x y\\a.exe" --background'
    assert "/RL" in create
    assert create[create.index("/RL") + 1] == "LIMITED"  # never elevated
    code["value"] = 1
    with pytest.raises(OSError, match="Access is denied"):
        tasks.create("x")
    assert tasks.exists() is False


# --------------------------------------------------------------------------- notifications
def test_new_request_toast_has_counts_only() -> None:
    report = IntakeReport(examined=2, requests=3, ready=1, needs_review=2)
    toast = for_check(report, scheduled=True)
    assert toast is not None
    assert toast.title == "3 new requests"
    assert "3 waiting" in toast.text
    assert "Nothing has been copied or sent" in toast.text


def test_single_request_is_singular_and_manual_checks_get_no_toast() -> None:
    one = for_check(IntakeReport(examined=1, requests=1, ready=1), scheduled=True)
    assert one is not None
    assert one.title == "1 new request"
    assert for_check(IntakeReport(examined=1, requests=1, ready=1), scheduled=False) is None


def test_quiet_check_has_no_toast_but_problems_do() -> None:
    assert for_check(IntakeReport(), scheduled=True) is None
    down = for_check(IntakeReport(errors=["could not list mail: X"]), scheduled=True)
    assert down is not None
    assert down.kind == "problem"
    sign_in = for_check(IntakeReport(needs_sign_in=True), scheduled=False)
    assert sign_in is not None
    assert sign_in.kind == "signin"
    assert sign_in.urgent


def test_copy_and_reply_toasts() -> None:
    ok = for_event(Event("copy_finished", 7, COPY_OK))
    assert ok is not None
    assert (ok.kind, ok.title) == ("copy", "Request #7: copy verified")
    failed = for_event(Event("copy_finished", 7, "Disk full"))
    assert failed is not None
    assert failed.kind == "problem"
    assert "No reply was sent" in failed.text
    assert "Disk full" not in failed.text  # the reason stays in the app
    assert for_event(Event("copy_finished", 7, "Cancelled")) is None
    problem = for_event(Event("reply_problem", 7, "Gmail said no"))
    assert problem is not None
    assert problem.urgent
    assert for_event(Event("changed", None, "")) is None
    assert for_event(Event("reply_sent", 7, "Reply sent")) is None


# --------------------------------------------------------------------------- single instance
def test_second_instance_asks_the_first_to_show_itself(qtbot: Any, tmp_path: Path) -> None:
    name = f"ContentShareTest-{uuid.uuid4().hex[:8]}"
    first = SingleInstance(tmp_path, name)
    second = SingleInstance(tmp_path, name)
    try:
        assert first.acquire()
        with qtbot.waitSignal(first.show_requested, timeout=5000):
            assert not second.acquire()

    finally:
        second.release()
        first.release()
    third = SingleInstance(tmp_path, name)  # the lock is free again after release
    try:
        assert third.acquire()
    finally:
        third.release()


# --------------------------------------------------------------------------- icon and tray
def test_icon_has_every_size_and_attention_dot() -> None:
    plain, dotted = app_icon(), app_icon(attention=True)
    assert not plain.isNull()
    assert {s.width() for s in plain.availableSizes()} >= {16, 32, 256}
    corner = (
        plain.pixmap(64).toImage().pixelColor(58, 5),
        dotted.pixmap(64).toImage().pixelColor(58, 5),
    )
    assert corner[0] != corner[1]


def test_tray_menu_signals_and_status(qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    tray = Tray()
    shown: list[tuple[Any, ...]] = []
    monkeypatch.setattr(tray.icon, "showMessage", lambda *a: shown.append(a))
    tray.set_status("Next check Tue 09:00", waiting=2)
    assert "2 waiting" in tray.icon.toolTip()
    assert "Next check" in tray.status_action.text()
    with qtbot.waitSignal(tray.open_requested):
        tray.open_action.trigger()
    with qtbot.waitSignal(tray.check_requested):
        tray.check_action.trigger()
    with qtbot.waitSignal(tray.quit_requested):
        tray.quit_action.trigger()
    tray.notify(Toast("Title", "Text", "info"))
    tray.notify(Toast("Bad", "News", "problem", urgent=True))
    assert shown[0][0:2] == ("Title", "Text")
    assert shown[1][3] > shown[0][3]  # problems stay up longer


# --------------------------------------------------------------------------- automation + window
class Rig:
    def __init__(self, world: MailWorld, tmp_path: Path, clock: Clock) -> None:
        self.world, self.clock = world, clock
        self.bridge = Bridge()
        self.controller = Controller(
            world.repo, world.index, world.config, gateway_factory=lambda: world.gateway,
            config_path=tmp_path / "config.yaml", today=lambda: TODAY,
            auth=SimpleNamespace(is_connected=lambda: True),  # type: ignore[arg-type]
        )  # fmt: skip
        self.controller.subscribe(self.bridge.forward)
        self.tray = Tray()
        self.toasts: list[Toast] = []
        self.tray.notify = self.toasts.append  # type: ignore[method-assign]
        self.tray.visible = lambda: True  # type: ignore[method-assign]
        self.automation = Automation(self.controller, self.bridge, DailyScheduler(now=clock))
        self.startup = StartAtLogin(FakeRegistry(), FakeTasks(), command="cmd")
        self.window = MainWindow(
            self.controller, self.bridge,
            tray=self.tray, automation=self.automation, startup=self.startup,
        )  # fmt: skip


@pytest.fixture
def rig(qtbot: Any, tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch) -> Iterator[Rig]:
    apply_theme(QApplication.instance())  # type: ignore[arg-type]
    monkeypatch.setattr(QApplication, "quit", staticmethod(lambda: None))  # keep the test run alive
    r = Rig(build_mail_world(tmp_path, db), tmp_path, Clock(at(3, 10)))
    qtbot.addWidget(r.window)
    yield r
    r.automation.stop()
    r.controller.shutdown()
    r.bridge.close()


def test_scheduled_check_runs_in_the_background_and_toasts(rig: Rig, qtbot: Any) -> None:
    rig.world.gmail.add(make_message(message_id="d1", subject="Sunrise Talk 12 Oct 2026"))
    rig.automation.start()
    rig.clock.advance(seconds=STARTUP_DELAY.total_seconds() + 1)
    with qtbot.waitSignal(rig.automation.checked, timeout=10000) as signal:
        rig.automation.poll()
    report, due = signal.args
    assert report.requests == 1
    assert due.reason == "missed"
    assert len(rig.window.requests._cards) == 1
    assert [t.title for t in rig.toasts] == ["1 new request"]
    assert "Scheduled check" in rig.window.statusBar().currentMessage()
    assert rig.automation.next_run() is not None
    assert "Next check" in rig.tray.status_action.text()


def test_scheduled_check_never_approves_or_sends(rig: Rig, qtbot: Any) -> None:
    rig.world.gmail.add(
        make_message(message_id="d2", subject="Sunrise Talk 12 Oct 2026", body="English please")
    )
    rig.automation.start()
    rig.clock.advance(minutes=1)
    with qtbot.waitSignal(rig.automation.checked, timeout=10000):
        rig.automation.poll()
    assert {v.state for v in rig.controller.views()} <= {"READY", "NEEDS_REVIEW"}
    assert rig.controller.repo.list_replies() == []
    assert rig.world.gmail.sent == []


def test_scheduled_check_failure_retries_and_toasts_a_problem(rig: Rig, qtbot: Any) -> None:
    def broken() -> Any:
        raise GmailError("down")

    rig.controller._gateway_factory = broken  # type: ignore[assignment]
    rig.automation.start()
    rig.clock.advance(minutes=1)
    with qtbot.waitSignal(rig.automation.checked, timeout=10000):
        rig.automation.poll()
    assert rig.automation.next_run() == rig.clock.t + RETRY_DELAYS[0]


def test_not_connected_is_reported_once_and_not_retried(rig: Rig, qtbot: Any) -> None:
    rig.controller.auth = SimpleNamespace(is_connected=lambda: False)  # type: ignore[assignment]
    rig.automation.start()
    rig.clock.advance(minutes=1)
    with qtbot.waitSignal(rig.automation.checked, timeout=5000) as signal:
        rig.automation.poll()
    assert signal.args[0].needs_sign_in
    assert [t.kind for t in rig.toasts] == ["signin"]
    nxt = rig.automation.next_run()
    assert nxt is not None
    assert nxt > rig.clock.t + timedelta(hours=1)


def test_a_manual_check_and_a_scheduled_one_never_overlap(rig: Rig) -> None:
    assert rig.controller._check_lock.acquire(blocking=False)
    try:
        report = rig.controller.check_now()
    finally:
        rig.controller._check_lock.release()
    assert report.already_running
    assert report.examined == 0


def test_toasts_respect_the_setting_and_an_active_window(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    toast = Toast("T", "x", "requests")
    rig.window._toast(toast)
    assert rig.toasts == [toast]
    monkeypatch.setattr(rig.window, "isActiveWindow", lambda: True)
    rig.window._toast(toast)
    assert len(rig.toasts) == 1  # the user is looking at the app already
    rig.window._toast(toast, force=True)
    assert len(rig.toasts) == 2
    monkeypatch.setattr(rig.window, "isActiveWindow", lambda: False)
    rig.controller.config = rig.controller.config.model_copy(
        update={
            "schedule": rig.controller.config.schedule.model_copy(update={"notifications": False})
        }
    )
    rig.window._toast(toast)
    assert len(rig.toasts) == 2


def test_close_button_hides_to_tray_and_tells_the_user_once(rig: Rig) -> None:
    rig.window.show()
    rig.window.close()
    assert rig.window.isHidden()
    assert [t.title for t in rig.toasts] == ["ContentShare is still running"]
    rig.window.show_window()
    assert rig.window.isVisible()
    rig.window.close()
    assert len(rig.toasts) == 1


def test_close_really_closes_when_the_tray_option_is_off(rig: Rig) -> None:
    rig.controller.config = rig.controller.config.model_copy(
        update={
            "schedule": rig.controller.config.schedule.model_copy(update={"close_to_tray": False})
        }
    )
    rig.window.show()
    rig.window.close()
    assert rig.toasts == []
    assert rig.window.isHidden()


def test_quit_from_the_tray_menu_quits_and_stops_the_schedule(rig: Rig) -> None:
    rig.automation.start()
    rig.window.show()
    rig.tray.quit_action.trigger()
    assert rig.window.isHidden()
    assert not rig.automation._timer.isActive()


def test_quit_asks_first_while_copies_run(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rig.controller, "busy", lambda: True)
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
    )
    rig.window.show()
    rig.window.quit_app()
    assert rig.window.isVisible()  # the user said no
    assert not rig.window._quitting
    rig.window.close()  # still the tray path afterwards
    assert rig.window.isHidden()


def test_show_window_can_jump_to_a_page(rig: Rig) -> None:
    rig.window.show_window("History")
    assert rig.window.stack.currentWidget() is rig.window.history


def test_settings_apply_start_with_windows_and_report_refusals(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, qtbot: Any
) -> None:
    page = rig.window.settings
    page.load()
    page.start_login.setChecked(True)
    page.check_time.setTime(page.check_time.time().addSecs(3600))
    with qtbot.waitSignal(page.saved):
        assert page.save()
    assert rig.startup.registry.get() == "cmd"
    assert "Starts with Windows" in page.start_note.text()
    assert rig.controller.config.schedule.start_with_windows

    def refuse(_wanted: bool) -> None:
        raise OSError("denied")

    page.start_login.setChecked(False)
    monkeypatch.setattr(rig.startup, "apply", refuse)
    assert page.save()  # the other settings are still saved
    assert "would not let ContentShare" in page.message.text()


def test_changing_settings_moves_the_next_check(rig: Rig, qtbot: Any) -> None:
    rig.automation.start()
    page = rig.window.settings
    page.load()
    page.check_time.setTime(page.check_time.time().fromString("23:30", "HH:mm"))
    assert page.save()
    nxt = rig.automation.next_run()
    assert nxt is not None
    assert (nxt.hour, nxt.minute) == (23, 30)
    page.daily_enabled.setChecked(False)
    assert page.save()
    assert rig.automation.next_run() is None
    assert "off" in rig.tray.status_action.text()


def test_command_line_flags() -> None:
    from contentshare.ui.app import parse_args

    args, rest = parse_args(["--background"])
    assert args.background
    assert rest == []
    args, rest = parse_args(["-platform", "offscreen"])  # Qt's own options pass through
    assert not args.background
    assert rest == ["-platform", "offscreen"]


def test_quit_request_is_separate_from_show(qtbot: Any, tmp_path: Path) -> None:
    name = f"ContentShareTest-{uuid.uuid4().hex[:8]}"
    first, client = SingleInstance(tmp_path, name), SingleInstance(tmp_path / "other", name)
    shown: list[str] = []
    first.show_requested.connect(lambda: shown.append("show"))
    try:
        assert first.acquire()
        with qtbot.waitSignal(first.quit_requested, timeout=5000):
            assert client.ask_to_quit()
        assert shown == []  # asking to quit does not also ask to show
        with qtbot.waitSignal(first.show_requested, timeout=5000):
            assert client.ask_to_show()
    finally:
        first.release()
    assert not client.ask_to_quit()  # nothing is listening any more
    assert not client.ask_to_show()


def test_new_command_line_switches() -> None:
    from contentshare.ui.app import parse_args

    args, _ = parse_args(["--selftest", "report.json", "--quit"])
    assert args.selftest == "report.json"
    assert args.quit
    assert parse_args(["--selftest"])[0].selftest == ""
    assert parse_args([])[0].selftest is None
    assert parse_args(["--remove-startup"])[0].remove_startup
    assert parse_args(["--version"])[0].version


def test_selftest_passes_from_source_and_writes_a_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    import keyring

    class WinVaultKeyring:  # the test suite swaps in a fake vault; the check wants the real name
        pass

    monkeypatch.setattr(keyring, "get_keyring", WinVaultKeyring)

    from contentshare.selftest import selftest

    report = tmp_path / "r.json"
    code = selftest(str(report))
    data = json.loads(report.read_text(encoding="utf-8"))
    failed = [c for c in data["checks"] if not c["ok"]]
    assert code == 0, failed
    assert data["ok"]
    assert {c["name"] for c in data["checks"]} >= {"verified copy", "google client", "database"}


def test_selftest_reports_a_failing_check(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from contentshare import selftest as st

    def broken() -> str:
        raise RuntimeError("DLL missing")

    monkeypatch.setattr(st, "CHECKS", [("good", lambda: "fine"), ("bad", broken)])
    report = tmp_path / "r.json"
    assert st.selftest(str(report)) == 1
    assert "DLL missing" in report.read_text(encoding="utf-8")


def test_installer_script_matches_the_program() -> None:
    from contentshare.ui.app import APP_USER_MODEL_ID

    iss = (Path(__file__).parent.parent / "installer" / "contentshare.iss").read_text(
        encoding="utf-8"
    )
    assert f'AppUserModelID: "{APP_USER_MODEL_ID}"' in iss
    assert "PrivilegesRequired=lowest" in iss  # never asks for administrator rights
    assert "--remove-startup" in iss
    assert "--quit" in iss

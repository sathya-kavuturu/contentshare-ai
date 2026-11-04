"""Runs the daily check on the GUI's clock. The decisions live in ``app/scheduler.py``."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from PySide6.QtCore import QObject, QTimer, Signal

from contentshare.app.controller import Controller
from contentshare.app.scheduler import DailyScheduler, Due
from contentshare.gmail.intake import IntakeReport
from contentshare.ui.bridge import Bridge

log = logging.getLogger(__name__)

POLL_MS = 15_000


class Automation(QObject):
    """Polls the scheduler and runs due checks in a worker thread."""

    # (report or None when the check itself crashed, why it ran)
    checked = Signal(object, object)

    def __init__(
        self,
        controller: Controller,
        bridge: Bridge,
        scheduler: DailyScheduler | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller, self.bridge = controller, bridge
        self.scheduler = scheduler or DailyScheduler()
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self.poll)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        c = self.controller.config
        self.scheduler.start(
            c.schedule.daily_check_time,
            last_check=self.controller.last_check(),
            run_missed=c.schedule.run_missed_check_on_startup,
            enabled=self._can_run(),
            every=self._every(),
        )
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def config_changed(self) -> None:
        c = self.controller.config
        self.scheduler.reconfigure(
            c.schedule.daily_check_time, enabled=self._can_run(), every=self._every()
        )

    def _can_run(self) -> bool:
        return self.controller.config.schedule.daily_check_enabled

    def _every(self) -> timedelta | None:
        minutes = self.controller.config.schedule.check_every_minutes
        return timedelta(minutes=minutes) if minutes else None

    # ------------------------------------------------------------------ running
    def next_run(self) -> datetime | None:
        return self.scheduler.next_run

    def poll(self) -> None:
        due = self.scheduler.poll()
        if due is None:
            return
        if not self.controller.gmail_connected():
            # Nothing to retry until the user connects; say so once, then wait for tomorrow.
            self.scheduler.finished(ok=False, needs_sign_in=True)
            self.checked.emit(IntakeReport(needs_sign_in=True), due)
            return
        self._run(due)

    def _run(self, due: Due) -> None:
        def done(report: IntakeReport) -> None:
            ok = not report.needs_sign_in and (report.examined > 0 or not report.errors)
            self.scheduler.finished(
                ok=ok or report.already_running, needs_sign_in=report.needs_sign_in
            )
            self.checked.emit(report, due)

        def failed(exc: Exception) -> None:
            log.warning("scheduled check failed: %s", type(exc).__name__)
            self.scheduler.finished(ok=False)
            self.checked.emit(None, due)

        self.bridge.work(self.controller.scheduled_check, done, failed)

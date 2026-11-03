"""When the daily mail check is due. No Qt, no threads: a clock goes in, a decision comes out.

The UI calls :meth:`DailyScheduler.poll` every few seconds. Because the decision is made from
the wall clock each time (never from a sleeping timer), a laptop that slept through the check
time simply runs it once when it wakes up.

A scheduled check only *reads* mail and creates request cards. It never approves, copies or
sends anything: those still need a click.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta

log = logging.getLogger(__name__)

# After a failed check (network down at login, Gmail unreachable) try again, then give up until
# the next day rather than hammering the service.
RETRY_DELAYS = (timedelta(minutes=2), timedelta(minutes=5), timedelta(minutes=15))
STARTUP_DELAY = timedelta(seconds=20)  # the network is often not up yet right after login


def parse_time(text: str) -> time:
    hour, minute = (int(part) for part in text.split(":"))
    return time(hour, minute)


def latest_occurrence(now: datetime, at: time) -> datetime:
    """The most recent moment at or before ``now`` that was ``at`` o'clock."""
    today = now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    return today if today <= now else today - timedelta(days=1)


def next_occurrence(now: datetime, at: time) -> datetime:
    """The first moment strictly after ``now`` that is ``at`` o'clock."""
    return latest_occurrence(now, at) + timedelta(days=1)


@dataclass(frozen=True)
class Due:
    """Why a check is being run now."""

    reason: str  # "scheduled" | "missed" | "retry"
    attempt: int = 0


class DailyScheduler:
    def __init__(self, *, now: Callable[[], datetime] = lambda: datetime.now().astimezone()):
        self._now = now
        self._at = time(9, 0)
        self._next: datetime | None = None
        self._pending: Due | None = None
        self._attempt = 0
        self._running = False
        self._enabled = False

    # ------------------------------------------------------------------ setup
    def start(
        self, at: str, *, last_check: datetime | None, run_missed: bool, enabled: bool = True
    ) -> None:
        """Begin scheduling. ``last_check`` decides whether today's check was already missed."""
        self._at = parse_time(at)
        self._enabled = enabled
        self._attempt = 0
        self._pending = None
        now = self._now()
        due_before = latest_occurrence(now, self._at)
        if run_missed and (last_check is None or last_check < due_before):
            # The check time passed while the app was closed: run once shortly after start.
            self._next = now + STARTUP_DELAY
            self._pending = Due("missed")
        else:
            self._next = next_occurrence(now, self._at)

    def reconfigure(self, at: str, *, enabled: bool = True) -> None:
        """The user changed the time. It never fires a check by itself, only moves the next one."""
        self._at = parse_time(at)
        self._enabled = enabled
        if self._running:
            return
        self._pending = None
        self._attempt = 0
        self._next = next_occurrence(self._now(), self._at)

    # ------------------------------------------------------------------ state
    @property
    def next_run(self) -> datetime | None:
        return self._next if self._enabled else None

    @property
    def running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------ driving
    def poll(self) -> Due | None:
        """Return a :class:`Due` when a check should start now (and mark it as running)."""
        if not self._enabled or self._running or self._next is None:
            return None
        if self._now() < self._next:
            return None
        due = self._pending or Due("scheduled")
        self._pending = None
        self._running = True
        return due

    def finished(self, *, ok: bool, needs_sign_in: bool = False) -> datetime:
        """Report the outcome of the check that :meth:`poll` started; returns the next run.

        A check that needs Gmail sign-in is not retried: asking again cannot help.
        """
        self._running = False
        now = self._now()
        if ok or needs_sign_in or self._attempt >= len(RETRY_DELAYS):
            if not ok:
                log.warning("giving up on this check until the next scheduled time")
            self._attempt = 0
            self._next = next_occurrence(now, self._at)
        else:
            delay = RETRY_DELAYS[self._attempt]
            self._attempt += 1
            self._pending = Due("retry", self._attempt)
            self._next = now + delay
        return self._next

"""When the automatic mail check is due (daily, or every so often). No Qt, no threads.

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
        self._every: timedelta | None = None  # None: once a day at ``_at``
        self._next: datetime | None = None
        self._pending: Due | None = None
        self._attempt = 0
        self._running = False
        self._enabled = False
        self._window: tuple[time, time] | None = None  # checks only happen between these times

    # ------------------------------------------------------------------ the allowed hours
    def _set_window(self, window: tuple[str, str] | None) -> None:
        self._window = (
            None if window is None or window[0] == window[1] else
            (parse_time(window[0]), parse_time(window[1]))
        )  # fmt: skip

    def _allowed(self, moment: datetime) -> bool:
        if self._window is None:
            return True
        start, end = self._window
        at = moment.time().replace(second=0, microsecond=0)
        return start <= at <= end if start <= end else at >= start or at <= end

    def _clamp(self, moment: datetime) -> datetime:
        """``moment``, or the next time the allowed hours begin if it falls outside them."""
        if self._allowed(moment) or self._window is None:
            return moment
        start = self._window[0]
        opening = moment.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
        return opening if opening > moment else opening + timedelta(days=1)

    # ------------------------------------------------------------------ setup
    def start(
        self,
        at: str,
        *,
        last_check: datetime | None,
        run_missed: bool,
        enabled: bool = True,
        every: timedelta | None = None,
        window: tuple[str, str] | None = None,
    ) -> None:
        """Begin scheduling. ``last_check`` decides whether a check was already missed.

        ``every`` makes it a check every so often instead of once a day at ``at``; ``window``
        (from, to) limits those checks to the hours between the two times."""
        self._at = parse_time(at)
        self._every = every
        self._set_window(window)
        self._enabled = enabled
        self._attempt = 0
        self._pending = None
        now = self._now()
        if every is not None:
            due = last_check + every if last_check else now
            if due <= now and run_missed:
                self._next = self._clamp(now + STARTUP_DELAY)  # overdue while the app was closed
                self._pending = Due("missed")
            else:
                self._next = self._clamp(due if due > now else now + every)
            return
        due_before = latest_occurrence(now, self._at)
        if run_missed and (last_check is None or last_check < due_before):
            # The check time passed while the app was closed: run once shortly after start.
            self._next = now + STARTUP_DELAY
            self._pending = Due("missed")
        else:
            self._next = next_occurrence(now, self._at)

    def reconfigure(
        self,
        at: str,
        *,
        enabled: bool = True,
        every: timedelta | None = None,
        window: tuple[str, str] | None = None,
    ) -> None:
        """The user changed the schedule. It never fires a check by itself, only moves the next."""
        self._at = parse_time(at)
        self._every = every
        self._set_window(window)
        self._enabled = enabled
        if self._running:
            return
        self._pending = None
        self._attempt = 0
        self._next = self._after(self._now())

    def _after(self, now: datetime) -> datetime:
        """The next regular check after one that ran (or was set up) at ``now``."""
        return self._clamp(now + self._every) if self._every else next_occurrence(now, self._at)

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
        now = self._now()
        if now < self._next:
            return None
        if self._every is not None and not self._allowed(now):
            self._next = self._clamp(now)  # outside the allowed hours: wait for them to begin
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
            self._next = self._after(now)
        else:
            delay = RETRY_DELAYS[self._attempt]
            self._attempt += 1
            self._pending = Due("retry", self._attempt)
            self._next = now + delay
        return self._next

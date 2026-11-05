"""A record of what the program did, for the Developer screen.

It keeps the Gmail calls of the last minutes and, for each mail check, the search that was
sent, the ids Gmail returned and the decision made about every message. Nothing is kept unless
developer mode is on. Sign-in tokens never reach it; mail text is not stored here at all (only
sender and subject, which the History shows anyway).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

MAX_CALLS = 500
MAX_CHECKS = 20
MAX_STEPS = 60


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


@dataclass
class GmailCall:
    at: str
    what: str
    millis: int
    ok: bool
    detail: str = ""

    def line(self) -> str:
        state = "ok" if self.ok else "FAILED"
        extra = f"  {self.detail}" if self.detail else ""
        return f"{self.at}  {self.what:<22} {self.millis:>5} ms  {state}{extra}"


@dataclass
class MessageTrace:
    message_id: str
    sender: str = ""
    subject: str = ""
    labels: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    outcome: str = "not decided"

    def lines(self) -> list[str]:
        out = [f"* {self.subject or '(no subject)'}  -  {self.sender or '?'}"]
        if self.labels:
            out.append(f"    Gmail labels: {', '.join(self.labels)}")
        out += [f"    {n}. {step}" for n, step in enumerate(self.steps, start=1)]
        out.append(f"    -> {self.outcome}")
        return out


@dataclass
class CheckTrace:
    started: str = field(default_factory=_now)
    searches: list[tuple[str, list[str]]] = field(default_factory=list)  # query, ids returned
    already_seen: list[str] = field(default_factory=list)
    forgotten: int = 0
    messages: dict[str, MessageTrace] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    summary: str = ""

    def text(self) -> str:
        out = [f"Mail check at {self.started}"]
        for query, ids in self.searches:
            out.append(f"Gmail search: {query}")
            out.append(f"  returned {len(ids)} message(s): {', '.join(ids[:20]) or 'none'}")
        if self.forgotten:
            out.append(f"Read again (left alone only for want of a department): {self.forgotten}")
        if self.already_seen:
            out.append(
                f"Already looked at before, so not fetched again: {len(self.already_seen)} "
                f"({', '.join(self.already_seen[:20])})"
            )
        if not self.messages:
            out.append("No new message was fetched.")
        for trace in self.messages.values():
            out.extend(trace.lines())
        out += [f"Note: {n}" for n in self.notes]
        if self.summary:
            out.append(self.summary)
        return "\n".join(out)


class DevTrace:
    def __init__(self) -> None:
        self.enabled = False
        self._lock = threading.Lock()
        self.calls: deque[GmailCall] = deque(maxlen=MAX_CALLS)
        self.checks: deque[CheckTrace] = deque(maxlen=MAX_CHECKS)
        self._current: CheckTrace | None = None

    # ------------------------------------------------------------------ gmail
    def call(self, what: str, started: float, ok: bool, detail: str = "") -> None:
        if not self.enabled:
            return
        millis = int((time.monotonic() - started) * 1000)
        with self._lock:
            self.calls.append(GmailCall(_now(), what, millis, ok, detail))

    # ------------------------------------------------------------------ checks
    def begin_check(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._current = CheckTrace()
            self.checks.append(self._current)

    def search(self, query: str, ids: list[str]) -> None:
        if self._current is not None and self.enabled:
            self._current.searches.append((query, list(ids)))

    def seen(self, ids: list[str]) -> None:
        if self._current is not None and self.enabled:
            self._current.already_seen = list(ids)

    def forgotten(self, count: int) -> None:
        if self._current is not None and self.enabled:
            self._current.forgotten = count

    def note(self, text: str) -> None:
        if self._current is not None and self.enabled:
            self._current.notes.append(text)

    def message(self, message_id: str, sender: str, subject: str, labels: list[str]) -> None:
        if self._current is not None and self.enabled:
            self._current.messages[message_id] = MessageTrace(
                message_id, sender, subject[:200], list(labels)
            )

    def step(self, message_id: str, text: str) -> None:
        if self._current is None or not self.enabled:
            return
        trace = self._current.messages.get(message_id)
        if trace is not None and len(trace.steps) < MAX_STEPS:
            trace.steps.append(text)

    def outcome(self, message_id: str, text: str) -> None:
        if self._current is None or not self.enabled:
            return
        trace = self._current.messages.get(message_id)
        if trace is not None:
            trace.outcome = text

    def end_check(self, summary: str) -> None:
        if self._current is not None and self.enabled:
            self._current.summary = summary
        self._current = None

    # ------------------------------------------------------------------ reading
    def last_check(self) -> CheckTrace | None:
        with self._lock:
            return self.checks[-1] if self.checks else None

    def call_lines(self) -> list[str]:
        with self._lock:
            return [c.line() for c in self.calls]

    def clear(self) -> None:
        with self._lock:
            self.calls.clear()
            self.checks.clear()
            self._current = None


TRACE = DevTrace()

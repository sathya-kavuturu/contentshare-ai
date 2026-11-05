"""The last log lines kept in memory, for the Developer screen. Uses the same safe format as the
log file, so an error message can never carry mail text into it."""

from __future__ import annotations

import copy
import logging
import threading
from collections import deque

from contentshare.logsafe import SafeFormatter

MAX_LINES = 3000
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class BufferHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.setFormatter(SafeFormatter(_FORMAT, "%H:%M:%S"))
        self._lines: deque[tuple[int, str]] = deque(maxlen=MAX_LINES)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            fresh = copy.copy(record)
            fresh.exc_text = None  # another handler may have cached an unsafe version
            text = self.format(fresh)
        except Exception:
            return
        with self._lock:
            self._lines.append((record.levelno, text))

    def lines(self, minimum: int = logging.DEBUG, contains: str = "") -> list[str]:
        needle = contains.casefold()
        with self._lock:
            return [t for lvl, t in self._lines if lvl >= minimum and needle in t.casefold()]

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()


BUFFER = BufferHandler()


def attach(debug: bool) -> None:
    """Start keeping log lines (idempotent). With ``debug`` the whole program logs every detail."""
    root = logging.getLogger()
    if BUFFER not in root.handlers:
        root.addHandler(BUFFER)
    if debug:
        root.setLevel(logging.DEBUG)
    elif root.level == logging.DEBUG:
        root.setLevel(logging.INFO)
    # noisy libraries stay quiet even in debug
    for name in ("googleapiclient", "httplib2", "urllib3", "PIL", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)


def detach() -> None:
    root = logging.getLogger()
    root.removeHandler(BUFFER)
    if root.level == logging.DEBUG:
        root.setLevel(logging.INFO)

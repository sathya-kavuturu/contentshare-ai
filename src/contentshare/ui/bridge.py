"""Moves work off the GUI thread and results back onto it.

Qt widgets may only be touched from the GUI thread, while the controller reports progress
from copy threads and Gmail calls block. ``Bridge`` is created in the GUI thread; emitting its
signals from any thread delivers the call on the GUI thread.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot

from contentshare.app.controller import Event

log = logging.getLogger(__name__)


class Bridge(QObject):
    # An ``Event`` from the controller, delivered on the GUI thread. (Not named ``event``:
    # that would shadow ``QObject.event``, which Qt calls for every event.)
    app_event = Signal(object)
    _call = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._call.connect(self._run)
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ui-work")

    @Slot(object)
    def _run(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:
            log.exception("error in a UI callback")

    def post(self, fn: Callable[[], None]) -> None:
        """Run ``fn`` on the GUI thread (safe to call from any thread)."""
        self._call.emit(fn)

    def forward(self, event: Event) -> None:
        """Controller listener: hand the event to the GUI thread."""
        self.app_event.emit(event)

    def work(
        self,
        job: Callable[[], Any],
        on_done: Callable[[Any], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        """Run ``job`` in a worker thread, then ``on_done(result)`` or ``on_error(exc)`` on the
        GUI thread."""

        def runner() -> None:
            try:
                result = job()
            except Exception as exc:
                log.exception("background job failed")
                if on_error:
                    self.post(lambda exc=exc: on_error(exc))  # type: ignore[misc]
                return
            if on_done:
                self.post(lambda: on_done(result))

        self._pool.submit(runner)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

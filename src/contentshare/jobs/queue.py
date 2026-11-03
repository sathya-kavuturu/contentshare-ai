"""Runs approved requests in the background, a few at a time, with pause / resume / cancel."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from contentshare.copy_engine import Control, Progress
from contentshare.jobs.copy_job import CopyJobRunner, JobOutcome


@dataclass
class JobHandle:
    request_id: int
    control: Control
    future: Future[JobOutcome]


class CopyQueue:
    def __init__(self, runner: CopyJobRunner, concurrency: int = 2) -> None:
        self._runner = runner
        self._pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="copy")
        self._handles: dict[int, JobHandle] = {}
        self._lock = threading.Lock()

    def submit(
        self,
        request_id: int,
        *,
        overwrite: frozenset[str] = frozenset(),
        progress: Callable[[Progress], None] | None = None,
    ) -> JobHandle:
        with self._lock:
            if request_id in self._handles:
                raise ValueError(f"Request {request_id} is already queued or running")
            control = Control()
            future = self._pool.submit(
                self._runner.run, request_id, control, progress=progress, overwrite=overwrite
            )
            handle = JobHandle(request_id, control, future)
            self._handles[request_id] = handle
        future.add_done_callback(lambda _f, rid=request_id: self._forget(rid))  # type: ignore[misc]
        return handle

    def _forget(self, request_id: int) -> None:
        with self._lock:
            self._handles.pop(request_id, None)

    def get(self, request_id: int) -> JobHandle | None:
        with self._lock:
            return self._handles.get(request_id)

    def pause(self, request_id: int) -> None:
        if handle := self.get(request_id):
            handle.control.pause()

    def resume(self, request_id: int) -> None:
        if handle := self.get(request_id):
            handle.control.resume()

    def cancel(self, request_id: int) -> None:
        if handle := self.get(request_id):
            handle.control.cancel()

    def active(self) -> list[int]:
        with self._lock:
            return sorted(self._handles)

    def shutdown(self, *, cancel: bool = False) -> None:
        if cancel:
            with self._lock:
                for handle in self._handles.values():
                    handle.control.cancel()
        self._pool.shutdown(wait=True)

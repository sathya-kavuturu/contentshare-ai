"""Read ahead: the next chunk is read (and hashed) while the current one is being written.

Copying one file is read, hash, write, one after the other. With a second thread reading the next
chunk while this one is written, the disk or network is kept busy in both directions, and the
time for a file is that of its slower side instead of the sum. Hashing runs on the reader thread
in the order of the file, so the digest is still that of the bytes in order.

The reader never runs more than ``len(views)`` chunks ahead, so memory stays bounded.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Generator, Sequence

DEFAULT_DEPTH = 3  # chunks in flight: one being read, one being used, one waiting
_LARGE_CHUNK = 16 * 1024 * 1024


def depth_for(chunk_bytes: int) -> int:
    """Fewer chunks in flight when each one is big, to keep memory use modest."""
    return DEFAULT_DEPTH if chunk_bytes <= _LARGE_CHUNK else 2


def read_ahead(
    read: Callable[[int], int],
    views: Sequence[memoryview],
    on_chunk: Callable[[memoryview], object] | None = None,
) -> Generator[memoryview]:
    """Yield the chunks of a file in order.

    ``read(i)`` fills ``views[i]`` and returns how many bytes it read (0 at the end).
    ``on_chunk`` (the hasher) sees each chunk on the reader thread before it is handed on. A
    yielded chunk is valid only until the next one is asked for. Errors on the reader thread are
    raised here. Close the generator (``contextlib.closing``) to stop the reader early.
    """
    free: queue.SimpleQueue[int | None] = queue.SimpleQueue()
    ready: queue.SimpleQueue[tuple[int, int] | BaseException] = queue.SimpleQueue()
    for i in range(len(views)):
        free.put(i)

    def reader() -> None:
        try:
            while (i := free.get()) is not None:
                n = read(i)
                if n and on_chunk is not None:
                    with views[i][:n] as piece:
                        on_chunk(piece)
                ready.put((i, n))
                if not n:
                    return
        except BaseException as exc:  # handed to the consuming thread
            ready.put(exc)

    thread = threading.Thread(target=reader, name="readahead", daemon=True)
    thread.start()
    try:
        while True:
            item = ready.get()
            if isinstance(item, BaseException):
                raise item
            i, n = item
            if not n:
                return
            with views[i][:n] as piece:
                yield piece
            free.put(i)
    finally:
        free.put(None)
        thread.join()

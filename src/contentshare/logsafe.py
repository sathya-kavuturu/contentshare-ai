"""A log format that cannot leak email text through an error message.

Python's default log output prints the *message* of every exception, and libraries sometimes
put the data they choked on into that message (a header value, a snippet of JSON, a line of
text). That would break the promise that email content never reaches the log file. This
formatter prints, for each exception in a chain, only its type and where it happened (file,
line, function), plus the numeric error codes of OS errors. The readable reasons shown to the
user in the app are separate and are not affected.
"""

from __future__ import annotations

import logging
import traceback
from pathlib import Path
from types import TracebackType


def describe_exception(
    exc_type: type[BaseException], exc: BaseException | None, tb: TracebackType | None
) -> str:
    lines = ["Traceback (messages omitted on purpose):"]
    for frame in traceback.extract_tb(tb):
        lines.append(f"  {Path(frame.filename).name}:{frame.lineno} in {frame.name}")
    head = exc_type.__name__
    if isinstance(exc, OSError):
        codes = [str(c) for c in (exc.errno, getattr(exc, "winerror", None)) if c is not None]
        if codes:
            head += f" (error code {', '.join(codes)})"
    lines.append(head)
    cause = exc.__cause__ or exc.__context__ if exc is not None else None
    if cause is not None and cause is not exc:
        lines.append("caused by:")
        lines.append(describe_exception(type(cause), cause, cause.__traceback__))
    return "\n".join(lines)


class SafeFormatter(logging.Formatter):
    def formatException(  # noqa: N802 - logging.Formatter override
        self,
        ei: tuple[type[BaseException], BaseException, TracebackType | None]
        | tuple[None, None, None],
    ) -> str:
        exc_type, exc, tb = ei
        if exc_type is None:
            return ""
        return describe_exception(exc_type, exc, tb)

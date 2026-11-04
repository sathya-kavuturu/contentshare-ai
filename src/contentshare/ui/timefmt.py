"""Times shown to the user, in the computer's own time zone.

Everything is stored as UTC (an ISO 8601 string); this is the one place that turns a stored
time into what the person sees on their clock.
"""

from __future__ import annotations

from datetime import UTC, datetime


def local_stamp(stored: object, *, seconds: bool = False, empty: str = "") -> str:
    """``2026-11-03 18:21`` in local time. A date without a time (``2026-11-03``) stays a date,
    and text that is not a time is returned as it is rather than hidden."""
    text = str(stored or "").strip()
    if not text:
        return empty
    if len(text) == 10:  # a plain date: there is no time to convert
        return text
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return text
    if when.tzinfo is None:  # stored times are UTC; an old row without a zone is read the same way
        when = when.replace(tzinfo=UTC)
    return when.astimezone().strftime("%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M")

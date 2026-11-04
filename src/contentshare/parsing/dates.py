"""Finding dates and date ranges in free text (and in folder names)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}  # fmt: skip
_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5,
    "sunday": 6,
}  # fmt: skip
_M = "|".join(sorted(_MONTHS, key=len, reverse=True))
_FLAGS = re.IGNORECASE
_DASHES = "-" + chr(0x2013) + chr(0x2014)  # hyphen, en dash, em dash
_ISO = re.compile(r"(?<!\d)(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")
_COMPACT = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")
_NUMERIC = re.compile(r"(?<!\d)(\d{1,2})[-/.](\d{1,2})[-/.](\d{4}|\d{2})(?!\d)")
_DAY_RANGE = re.compile(
    rf"(?<!\d)(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:[{_DASHES}]|to|till|until|through)\s*(\d{{1,2}})"
    rf"(?:st|nd|rd|th)?\s+({_M})\b\.?(?:,?\s*(\d{{4}}))?",
    _FLAGS,
)
_DAY_MONTH = re.compile(
    rf"(?<!\d)(\d{{1,2}})(?:st|nd|rd|th)?[\s\-.]*(?:of\s+)?({_M})\b\.?(?:,?\s*[-.]?\s*(\d{{4}}))?",
    _FLAGS,
)
_MONTH_DAY = re.compile(
    rf"\b({_M})\b\.?\s*(\d{{1,2}})(?:st|nd|rd|th)?(?!\d)(?:,?\s*(\d{{4}}))?", _FLAGS
)
_RELATIVE = re.compile(
    r"\b(day before yesterday|yesterday|today|last\s+(?:" + "|".join(_WEEKDAYS) + r"))\b", _FLAGS
)
_RANGE_JOINER = re.compile(rf"^\s*(?:[{_DASHES}]|to|till|until|through)\s*$", _FLAGS)


@dataclass(frozen=True)
class DateRef:
    start: date
    end: date
    span: tuple[int, int] = (0, 0)
    alt: tuple[date, date] | None = None  # the other reading of an ambiguous 03/04/2026
    year_guessed: bool = False  # written without a year ("12 Oct"), so the year is our guess

    def options(self) -> list[tuple[date, date]]:
        return [(self.start, self.end), *([self.alt] if self.alt else [])]

    def overlaps(self, other: DateRef) -> bool:
        return any(a0 <= b1 and b0 <= a1 for a0, a1 in self.options() for b0, b1 in other.options())

    def label(self) -> str:
        if self.start == self.end:
            return self.start.isoformat()
        return f"{self.start.isoformat()}..{self.end.isoformat()}"


def same_days(dates: list[DateRef]) -> list[DateRef]:
    """Each day once. "12 Oct" in the subject and "12 October 2025" in the body are one day:
    a date whose year was only guessed is dropped when the same day is also written with a year."""

    def key(d: DateRef) -> tuple[int, int, int, int]:
        return (d.start.month, d.start.day, d.end.month, d.end.day)

    stated = {key(d) for d in dates if not d.year_guessed}
    kept = [d for d in dates if not (d.year_guessed and key(d) in stated)]
    return list({(d.start, d.end): d for d in kept}.values())


def _make(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _infer_year(month: int, day: int, today: date) -> date | None:
    """A date without a year refers to the most recent such date, not one far in the future."""
    guess = _make(today.year, month, day)
    if guess is None:
        return None
    return guess if guess <= today + timedelta(days=30) else _make(today.year - 1, month, day)


def _year(text: str | None) -> int | None:
    if not text:
        return None
    value = int(text)
    return 2000 + value if value < 100 else value


class _Found:
    def __init__(self) -> None:
        self.refs: list[DateRef] = []
        self._taken: list[tuple[int, int]] = []

    def free(self, span: tuple[int, int]) -> bool:
        return all(span[1] <= a or span[0] >= b for a, b in self._taken)

    def add(self, span: tuple[int, int], start: date, end: date | None = None,
            alt: tuple[date, date] | None = None, guessed: bool = False) -> None:  # fmt: skip
        if self.free(span):
            self._taken.append(span)
            self.refs.append(DateRef(start, end or start, span, alt, guessed))


def _numeric(match: re.Match[str], day_first: bool) -> tuple[date, tuple[date, date] | None] | None:
    """Read ``03/04/2026``. If both readings are valid dates, the preferred one is returned
    together with the other as the alternative, and the caller treats either as a match."""
    a, b, year = int(match[1]), int(match[2]), _year(match[3])
    if year is None:
        return None
    as_dm, as_md = _make(year, b, a), _make(year, a, b)
    if a > 12:
        return (as_dm, None) if as_dm else None
    if b > 12:
        return (as_md, None) if as_md else None
    first, second = (as_dm, as_md) if day_first else (as_md, as_dm)
    if first is None:
        return (second, None) if second else None
    alt = (second, second) if second and second != first else None
    return first, alt


def _scan_numeric_forms(text: str, found: _Found, day_first: bool) -> None:
    for m in _ISO.finditer(text):
        if d := _make(int(m[1]), int(m[2]), int(m[3])):
            found.add(m.span(), d)
    for m in _COMPACT.finditer(text):
        if d := _make(int(m[1]), int(m[2]), int(m[3])):
            found.add(m.span(), d)
    for m in _NUMERIC.finditer(text):
        if (res := _numeric(m, day_first)) and found.free(m.span()):
            found.add(m.span(), res[0], None, res[1])


def _day_in_month(month: int, day: int, year: int | None, today: date) -> date | None:
    return _make(year, month, day) if year else _infer_year(month, day, today)


def _scan_textual_forms(text: str, found: _Found, today: date) -> None:
    for m in _DAY_RANGE.finditer(text):
        month, year = _MONTHS[m[3].lower()], _year(m[4])
        lo = _day_in_month(month, int(m[1]), year, today)
        hi = _day_in_month(month, int(m[2]), year, today)
        if lo and hi and lo <= hi:
            found.add(m.span(), lo, hi, guessed=year is None)
    for m in _DAY_MONTH.finditer(text):
        if d := _day_in_month(_MONTHS[m[2].lower()], int(m[1]), _year(m[3]), today):
            found.add(m.span(), d, guessed=not m[3])
    for m in _MONTH_DAY.finditer(text):
        if d := _day_in_month(_MONTHS[m[1].lower()], int(m[2]), _year(m[3]), today):
            found.add(m.span(), d, guessed=not m[3])
    for m in _RELATIVE.finditer(text):
        if d := _relative(m[1].lower(), today):
            found.add(m.span(), d)


def find_dates(text: str, *, day_first: bool = True, today: date | None = None) -> list[DateRef]:
    """All dates and ranges in ``text``, in reading order."""
    today = today or date.today()
    found = _Found()
    _scan_numeric_forms(text, found, day_first)
    _scan_textual_forms(text, found, today)
    return _merge_ranges(text, sorted(found.refs, key=lambda r: r.span))


def _relative(word: str, today: date) -> date | None:
    if word == "today":
        return today
    if word == "yesterday":
        return today - timedelta(days=1)
    if word == "day before yesterday":
        return today - timedelta(days=2)
    weekday = _WEEKDAYS[word.rsplit(maxsplit=1)[-1]]
    back = (today.weekday() - weekday) % 7 or 7  # "last Sunday" is never today
    return today - timedelta(days=back)


def _merge_ranges(text: str, refs: list[DateRef]) -> list[DateRef]:
    """``12 Oct - 14 Oct`` and ``from 03/04/2026 to 05/04/2026`` become one range."""
    merged: list[DateRef] = []
    for ref in refs:
        if merged:
            prev = merged[-1]
            between = text[prev.span[1] : ref.span[0]]
            if prev.start == prev.end and ref.start == ref.end and _RANGE_JOINER.match(between):
                lo, hi = sorted((prev.start, ref.start))
                merged[-1] = DateRef(lo, hi, (prev.span[0], ref.span[1]))
                continue
        merged.append(ref)
    return merged


def remove_spans(text: str, refs: list[DateRef]) -> str:
    for ref in sorted(refs, key=lambda r: r.span, reverse=True):
        text = text[: ref.span[0]] + " " + text[ref.span[1] :]
    return text


_MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)  # fmt: skip
MAX_PHRASE_DAYS = 31


def date_phrases(refs: list[DateRef]) -> list[str]:
    """Search phrases for the ways a folder name might spell the requested dates, as the
    search index sees them (words and numbers split apart): ``2026-10-12`` -> ``2026 10 12``,
    ``20261012`` -> ``20261012``, ``12 Oct 2026`` -> ``12 oct 2026`` and so on. Both readings
    of an ambiguous date are included. Long ranges are capped."""
    days: list[date] = []
    for ref in refs:
        for start, end in ref.options():
            span = min((end - start).days, MAX_PHRASE_DAYS - 1)
            days.extend(start + timedelta(days=n) for n in range(span + 1))
    phrases: dict[str, None] = {}
    for d in days:
        y, m, dd = d.year, d.month, d.day
        month = _MONTH_NAMES[m - 1]
        for variant in (
            f"{y} {m:02d} {dd:02d}", f"{y} {m} {dd}",
            f"{dd:02d} {m:02d} {y}", f"{dd} {m} {y}",
            f"{y}{m:02d}{dd:02d}", f"{dd:02d}{m:02d}{y}",
            f"{dd} {month[:3]} {y}", f"{dd:02d} {month[:3]} {y}", f"{dd} {month} {y}",
            f"{month[:3]} {dd} {y}", f"{month} {dd} {y}",
            f"{dd}{month[:3]}{y}", f"{dd:02d}{month[:3]}{y}",
        ):  # fmt: skip
            phrases.setdefault(variant)
    return list(phrases)

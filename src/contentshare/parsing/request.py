"""Turn a request email (subject + plain-text body) into one or more request items.

Deterministic rules only: no network, no model, nothing is executed. Whatever the email says
ends up as plain data (program words, dates, language, part, formats) that the matcher then
compares against the index. Text from an email never becomes a file path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, NamedTuple

from contentshare.config.models import ParsingSettings
from contentshare.parsing.attributes import (
    find_formats,
    find_languages,
    find_parts,
    program_words,
)
from contentshare.parsing.dates import DateRef, find_dates, remove_spans, same_days
from contentshare.parsing.text import clean_body, clean_subject, strip_links

MAX_ITEMS = 20
MAX_SUBJECT_CHARS = 300  # the Gmail reader already cuts it here; this keeps the parser safe alone
_INTRO_LINES = 6
_PROSE_LINES = 2
_BULLET_CHARS = "-*" + chr(0x2022) + chr(0x2013) + chr(0x2014)  # - * bullet en-dash em-dash
_QUOTE_OPEN = '"' + chr(0x201C) + chr(0x2018)
_QUOTE_CLOSE = '"' + chr(0x201D) + chr(0x2019)
_BULLET = re.compile(rf"^\s*(?:[{re.escape(_BULLET_CHARS)}]|\d{{1,2}}[.)])\s+(.+)$")
_QUOTED = re.compile(rf"(?<!\w)[{_QUOTE_OPEN}]([^{_QUOTE_CLOSE}\n]{{3,80}})[{_QUOTE_CLOSE}](?!\w)")


@dataclass(frozen=True)
class RequestItem:
    program: str
    dates: tuple[DateRef, ...] = ()
    language: str | None = None
    part: tuple[str, int] | None = None
    formats: tuple[str, ...] = ()
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "program": self.program,
            "dates": [
                [
                    d.start.isoformat(),
                    d.end.isoformat(),
                    *([a.isoformat() for a in d.alt] if d.alt else []),
                ]
                for d in self.dates
            ],
            "language": self.language,
            "part": list(self.part) if self.part else None,
            "formats": list(self.formats),
            "raw": self.raw,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RequestItem:
        dates = []
        for entry in data.get("dates", []):
            start, end = date.fromisoformat(entry[0]), date.fromisoformat(entry[1])
            alt = (
                (date.fromisoformat(entry[2]), date.fromisoformat(entry[3]))
                if len(entry) > 2
                else None
            )
            dates.append(DateRef(start, end, alt=alt))
        part = data.get("part")
        return cls(
            program=data.get("program", ""),
            dates=tuple(dates),
            language=data.get("language"),
            part=(part[0], int(part[1])) if part else None,
            formats=tuple(data.get("formats", [])),
            raw=data.get("raw", ""),
        )


@dataclass(frozen=True)
class ParsedRequest:
    items: tuple[RequestItem, ...]
    notes: tuple[str, ...] = ()


class _Attrs(NamedTuple):
    dates: list[DateRef]
    language: str | None
    part: tuple[str, int] | None
    formats: tuple[str, ...]
    rest: str  # the text with all of the above removed


def _blank(text: str, spans: list[tuple[int, int]]) -> str:
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + " " * (end - start) + text[end:]
    return text


def _extract(text: str, settings: ParsingSettings, today: date) -> _Attrs:
    text = text.replace("_", " ")
    dates = find_dates(text, day_first=settings.day_first, today=today)
    rest = remove_spans(text, dates)
    langs = find_languages(rest, settings.languages, strict_codes=True)
    rest = _blank(rest, [span for _, span in langs])
    parts = find_parts(rest)
    rest = _blank(rest, [span for _, _, span in parts])
    fmts = find_formats(rest)
    rest = _blank(rest, [span for _, span in fmts])
    return _Attrs(
        dates,
        langs[0][0] if langs else None,
        (parts[0][0], parts[0][1]) if parts else None,
        tuple(dict.fromkeys(cat for cat, _ in fmts)),
        rest,
    )


def _program_text(raw: str, rest: str, settings: ParsingSettings) -> str:
    if m := _QUOTED.search(raw):
        return m[1].strip()
    for pattern in settings.program_patterns:
        if m := re.search(pattern, raw, re.IGNORECASE):
            return m.group("program").strip()[:120]
    return program_words(rest, settings.stop_phrases)[:120]


def _make_items(
    program: str,
    dates: list[DateRef],
    language: str | None,
    part: tuple[str, int] | None,
    formats: tuple[str, ...],
    raw: str,
) -> list[RequestItem]:
    # The subject and the body often both give the date: count the same day only once.
    dates = same_days(dates)
    if len(dates) > 1:  # "12 Oct and 19 Oct": one request line per date
        return [RequestItem(program, (d,), language, part, formats, raw) for d in dates]
    return [RequestItem(program, tuple(dates), language, part, formats, raw)]


def _bullet_items(
    raw: str, ctx: _Attrs, ctx_program: str, settings: ParsingSettings, today: date
) -> list[RequestItem]:
    """A listed item uses its own details and falls back to the rest of the email for the rest."""
    own = _extract(raw, settings, today)
    return _make_items(
        _program_text(raw, own.rest, settings) or ctx_program,
        own.dates or ctx.dates,
        own.language or ctx.language,
        own.part or ctx.part,
        own.formats or ctx.formats,
        raw[:200],
    )


def parse_request(
    subject: str, body: str, settings: ParsingSettings | None = None, *, today: date | None = None
) -> ParsedRequest:
    settings = settings or ParsingSettings()
    today = today or date.today()
    subj = clean_subject(strip_links(subject[:MAX_SUBJECT_CHARS]))
    lines = clean_body(body).split(chr(10)) if body.strip() else []
    bullets = [m[1].strip() for line in lines if (m := _BULLET.match(line))]
    intro = [line for line in lines if not _BULLET.match(line)][:_INTRO_LINES]

    ctx = _extract(" ".join([subj, *intro]), settings, today)
    prose = " ".join(intro[:_PROSE_LINES])
    # The subject names the program if it can; otherwise the first lines of the body do.
    ctx_program = _program_text(
        subj, _extract(subj, settings, today).rest, settings
    ) or _program_text(prose, _extract(prose, settings, today).rest, settings)

    notes: list[str] = []
    if bullets:
        if len(bullets) > MAX_ITEMS:
            notes.append(f"Only the first {MAX_ITEMS} of {len(bullets)} listed items were read")
        items = [
            item
            for raw in bullets[:MAX_ITEMS]
            for item in _bullet_items(raw, ctx, ctx_program, settings, today)
        ]
    else:
        raw = " ".join(filter(None, [subj, prose]))[:200]
        items = _make_items(ctx_program, ctx.dates, ctx.language, ctx.part, ctx.formats, raw)
    if len(items) > MAX_ITEMS:
        notes.append(f"Only the first {MAX_ITEMS} of {len(items)} requested items were read")
        items = items[:MAX_ITEMS]
    return ParsedRequest(tuple(items), tuple(notes))

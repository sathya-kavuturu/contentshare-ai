"""Exact folder names: for departments whose requests give the folder name as it is on the server.
(A file is found the same way, by its name with or without its extension.)

Nothing is guessed here. The email is read for names (one per line, in a list, in quotes, or as a
single word that looks like a folder name such as ``2026-10-12_SunriseTalk_EN``), and each name
is looked up in the storage index as a folder with exactly that name (capital letters do not
matter, as in Windows). Text from an email is only ever compared with names; it never becomes a
path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from contentshare.index.store import IndexedItem
from contentshare.parsing.dates import MONTH_PATTERN, find_dates
from contentshare.parsing.text import clean_body, clean_subject, strip_links

MAX_NAMES = 60  # names looked up per email
MAX_NAME_CHARS = 200
_DASHES = chr(0x2013) + chr(0x2014)  # en dash, em dash
_OPEN_QUOTES = '"' + chr(0x201C)
_CLOSE_QUOTES = '"' + chr(0x201D)
_EDGE = " \t.,:;!?-*\"'()[]" + _DASHES + chr(0x2018) + chr(0x2019) + chr(0x201C) + chr(0x201D)
_BULLET = re.compile(rf"^\s*(?:[-*{chr(0x2022)}{_DASHES}]|\d{{1,2}}[.)])\s+(.+)$")
_QUOTED = re.compile(rf"[{_OPEN_QUOTES}]([^{_CLOSE_QUOTES}\n]{{3,200}})[{_CLOSE_QUOTES}]")
_SPLIT = re.compile(r"[,;|]")
_WORD = re.compile(rf"[^\s,;|()\[\]{{}}<>{_OPEN_QUOTES}{chr(0x201D)}]+")
_NAME_LIKE = re.compile(r"^[\w.\-]{5,200}$")
# "Folder name: X", "Initiation file name: X", "File: X": a label that says what the name is
_LABELLED = re.compile(
    r"^\s*(?:[A-Za-z][A-Za-z ]{0,40}\s)?(?:folders?|files?)\b[A-Za-z ]{0,20}:\s*(?P<name>\S.*)$",
    re.IGNORECASE,
)
_DAY = r"\d{1,2}(?:st|nd|rd|th)?"
_RANGE = rf"\s*[-{_DASHES}]\s*"
# "Nov 22-28 ", "Nov 22 - Nov 28, 2026: ", "22-28 Nov 2026 - " at the start of a line: the week or
# day a request is for, written before the name of the folder.
_LEADING_DATE = re.compile(
    rf"^(?:(?:{MONTH_PATTERN})\b\.?\s*{_DAY}(?:{_RANGE}(?:(?:{MONTH_PATTERN})\b\.?\s*)?{_DAY})?"
    rf"|{_DAY}(?:{_RANGE}{_DAY})?\s+(?:of\s+)?(?:{MONTH_PATTERN})\b\.?)"
    rf"(?:,?\s*\d{{4}}\b)?\s*[-{_DASHES}:|,]?\s+(?=\S)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class WantedName:
    name: str
    explicit: bool  # a list item, a quoted name or a folder-looking word: report it if missing
    # What is left when a date written before the name is dropped ("Nov 22-28 Amber Step 4 Hindi
    # v002" is the folder "Amber Step 4 Hindi v002"). Tried only when nothing is named exactly
    # ``name``.
    alternates: tuple[str, ...] = ()


def without_leading_date(name: str) -> str | None:
    """``name`` without a date or date range before it, or ``None`` if it does not start with one
    (or nothing would be left). A date after the name is kept: it is part of the folder's name."""
    match = _LEADING_DATE.match(name)
    rest = _tidy(name[match.end() :]) if match else ""
    return rest if len(rest) >= 3 else None


def _tidy(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip(_EDGE).strip()


def _looks_like_a_folder_name(word: str) -> bool:
    """``Sunrise_Talk`` or ``Talk2026`` is probably a folder name; ``please`` is not."""
    if not _NAME_LIKE.match(word) or not re.search(r"[^\W\d_]", word):
        return False  # too short, or no letters (a number, a phone number)
    if find_dates(word):
        return False
    return "_" in word or bool(re.search(r"\d", word))


def wanted_names(subject: str, body: str) -> list[WantedName]:
    """The names in an email that might be folder names, most explicit first."""
    found: dict[str, WantedName] = {}

    def add(raw: str, explicit: bool) -> None:
        name = _tidy(raw)[:MAX_NAME_CHARS]
        if len(name) < 3 or len(found) >= MAX_NAMES:
            return
        key = name.casefold()
        previous = found.get(key)
        if previous is None or (explicit and not previous.explicit):
            shorter = without_leading_date(name)
            # a line that starts with a date is a listed name ("Nov 22-28 Amber Step 4 ..."): if
            # there is no such folder it is reported, never dropped
            found[key] = WantedName(name, explicit or bool(shorter), (shorter,) if shorter else ())

    lines = [clean_subject(strip_links(subject))] if subject.strip() else []
    lines += clean_body(body).split("\n") if body.strip() else []
    for number, line in enumerate(lines):
        bullet = _BULLET.match(line)
        text = bullet[1] if bullet else line
        if labelled := _LABELLED.match(text):
            # "Folder name: X" and "Initiation file name: Y" are two separate things asked for
            add(labelled["name"], True)
        heading = bullet and number + 1 < len(lines) and _LABELLED.match(lines[number + 1])
        if heading:
            continue  # "1. English" above its labelled lines only introduces them
        for quoted in _QUOTED.findall(text):
            add(quoted, True)
        add(text, bool(bullet))  # the whole line, for names that contain spaces
        if _SPLIT.search(text):
            for piece in _SPLIT.split(text):
                add(piece, bool(bullet))
        for word in _WORD.findall(text):
            cleaned = _tidy(word)
            if _looks_like_a_folder_name(cleaned):  # a plain word such as "2026" or "video" is not
                add(cleaned, True)
    return sorted(found.values(), key=lambda w: not w.explicit)


_CHATTER = re.compile(
    r"^(?:hi|hello|hey|dear|namaste|namaskaram|pranam|thanks?|thank you|regards|warm regards|"
    r"best|sincerely|with love|pranams|good (?:morning|afternoon|evening))\b",
    re.IGNORECASE,
)
# "On Wed, Nov 4, 2026 at 3:59 PM Some Name" (the start of a quoted reply)
_QUOTE_HEADER = re.compile(r"^on\s+.{3,120}\d{1,2}:\d{2}", re.IGNORECASE)


def unread_lines(body: str, covered: list[str]) -> list[str]:
    """Lines of the email body that no request was made from, apart from greetings, thanks and
    lines ending in a colon. They are shown to the person so that a name that was skipped is
    never invisible. (The subject is on the card already.)"""
    done = [c.casefold() for c in covered if c]
    out: list[str] = []
    for line in clean_body(body).split("\n") if body.strip() else []:
        bullet = _BULLET.match(line)
        raw = bullet[1] if bullet else line
        text = _tidy(raw)
        if (
            len(text) < 8
            or raw.rstrip().endswith(":")
            or _CHATTER.match(text)
            or _QUOTE_HEADER.match(text)
        ):
            continue
        if any(d in text.casefold() for d in done) or text in out:
            continue
        out.append(text[:MAX_NAME_CHARS])
    return out


def drop_enclosing(matches: dict[str, list[IndexedItem]]) -> dict[str, list[IndexedItem]]:
    """If the email names a folder and also a folder inside it, the inner one is meant: the
    outer one is only the place it sits in."""
    every = [item for items in matches.values() for item in items]

    def encloses_another(item: IndexedItem) -> bool:
        prefix = item.rel_path + "/"
        return any(o.root == item.root and o.rel_path.startswith(prefix) for o in every)

    kept = {name: [i for i in items if not encloses_another(i)] for name, items in matches.items()}
    return {name: items for name, items in kept.items() if items}

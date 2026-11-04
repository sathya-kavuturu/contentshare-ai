"""Exact folder names: for departments whose requests give the folder name as it is on the server.

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
from contentshare.parsing.dates import find_dates
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


@dataclass(frozen=True)
class WantedName:
    name: str
    explicit: bool  # a list item, a quoted name or a folder-looking word: report it if missing


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
            found[key] = WantedName(name, explicit)

    lines = [clean_subject(strip_links(subject))] if subject.strip() else []
    lines += clean_body(body).split("\n") if body.strip() else []
    for line in lines:
        bullet = _BULLET.match(line)
        text = bullet[1] if bullet else line
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


def drop_enclosing(matches: dict[str, list[IndexedItem]]) -> dict[str, list[IndexedItem]]:
    """If the email names a folder and also a folder inside it, the inner one is meant: the
    outer one is only the place it sits in."""
    every = [item for items in matches.values() for item in items]

    def encloses_another(item: IndexedItem) -> bool:
        prefix = item.rel_path + "/"
        return any(o.root == item.root and o.rel_path.startswith(prefix) for o in every)

    kept = {name: [i for i in items if not encloses_another(i)] for name, items in matches.items()}
    return {name: items for name, items in kept.items() if items}

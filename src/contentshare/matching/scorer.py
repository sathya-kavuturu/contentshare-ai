"""Scoring of indexed folders/files against one request item. Pure: no database, no disk."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from rapidfuzz import fuzz

from contentshare.config.models import ParsingSettings
from contentshare.index.store import IndexedItem
from contentshare.parsing.attributes import (
    find_formats,
    find_languages,
    find_parts,
    format_of_extension,
)
from contentshare.parsing.dates import find_dates, remove_spans
from contentshare.parsing.request import RequestItem
from contentshare.parsing.text import normalize_words

MIN_SHOWN = 40.0  # candidates scoring below this are not worth showing
PATH_FACTOR = 0.95  # a name matched through a parent folder counts slightly less
MISMATCH_FLAGS = frozenset(
    {"date_mismatch", "language_mismatch", "part_mismatch", "format_mismatch"}
)

BONUS = {"date": 10.0, "language": 6.0, "part": 8.0, "format": 3.0}
PENALTY = {"date": 30.0, "language": 25.0, "part": 25.0, "format": 10.0}
UNCONFIRMED_DATE = 5.0  # the request names a date but the folder shows none


@dataclass(frozen=True)
class Candidate:
    item: IndexedItem
    score: float  # 0..100, for display
    raw_score: float  # unclamped, for ordering and gaps
    name_score: float
    notes: tuple[str, ...]
    flags: frozenset[str]

    @property
    def has_mismatch(self) -> bool:
        return bool(self.flags & MISMATCH_FLAGS)

    def to_dict(self) -> dict[str, object]:
        return {
            "item_id": self.item.id,
            "root": self.item.root,
            "rel_path": self.item.rel_path,
            "path": self.item.full_path,
            "kind": self.item.kind,
            "size": self.item.size,
            "file_count": self.item.file_count,
            "score": round(self.score, 1),
            "notes": list(self.notes),
        }


def _blank(text: str, spans: list[tuple[int, int]]) -> str:
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + " " * (end - start) + text[end:]
    return text


def name_words(text: str, settings: ParsingSettings, today: date) -> str:
    """The part of a name that identifies the program: dates, languages, part numbers and
    format words are removed because they are compared separately."""
    text = text.replace("_", " ")
    text = remove_spans(text, find_dates(text, day_first=settings.day_first, today=today))
    text = _blank(
        text, [s for _, s in find_languages(text, settings.languages, strict_codes=False)]
    )
    text = _blank(text, [s for _, _, s in find_parts(text)])
    text = _blank(text, [s for _, s in find_formats(text)])
    return normalize_words(text)


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return 0.5 * fuzz.token_set_ratio(a, b) + 0.5 * fuzz.token_sort_ratio(a, b)


def _date_adjust(req: RequestItem, context: str, settings: ParsingSettings, today: date,
                 flags: set[str], notes: list[str]) -> float:  # fmt: skip
    if not req.dates:
        return 0.0
    found = find_dates(context, day_first=settings.day_first, today=today)
    if not found:
        flags.add("date_unknown")
        notes.append("no date in the name to confirm")
        return -UNCONFIRMED_DATE
    if any(c.overlaps(r) for c in found for r in req.dates):
        flags.add("date_match")
        notes.append("date matches")
        return BONUS["date"]
    flags.add("date_mismatch")
    notes.append(f"different date ({found[0].label()})")
    return -PENALTY["date"]


def _language_adjust(req: RequestItem, context: str, settings: ParsingSettings,
                     flags: set[str], notes: list[str]) -> float:  # fmt: skip
    if not req.language:
        return 0.0
    found = {name for name, _ in find_languages(context, settings.languages, strict_codes=False)}
    if req.language in found:
        flags.add("language_match")
        notes.append(f"{req.language} matches")
        return BONUS["language"]
    if found:
        flags.add("language_mismatch")
        notes.append(f"different language ({', '.join(sorted(found))})")
        return -PENALTY["language"]
    return 0.0


def _part_adjust(req: RequestItem, context: str, flags: set[str], notes: list[str]) -> float:
    if not req.part:
        return 0.0
    kind, number = req.part
    same_kind = [n for k, n, _ in find_parts(context) if k == kind]
    if not same_kind:
        return 0.0
    if number in same_kind:
        flags.add("part_match")
        notes.append(f"{kind} {number} matches")
        return BONUS["part"]
    flags.add("part_mismatch")
    notes.append(f"different {kind} ({same_kind[0]})")
    return -PENALTY["part"]


def _format_adjust(req: RequestItem, item: IndexedItem, flags: set[str], notes: list[str]) -> float:
    if not req.formats or item.is_dir:
        return 0.0
    category = format_of_extension(item.name)
    if category is None:
        return 0.0
    if category in req.formats:
        flags.add("format_match")
        return BONUS["format"]
    flags.add("format_mismatch")
    notes.append(f"is {category}, not {'/'.join(req.formats)}")
    return -PENALTY["format"]


def score_item(
    req: RequestItem, item: IndexedItem, settings: ParsingSettings, *, today: date
) -> Candidate:
    query = name_words(req.program, settings, today)
    leaf_text = item.name.rsplit(".", 1)[0] if not item.is_dir and "." in item.name else item.name
    context = item.rel_path.replace("/", " ")

    name_score = similarity(query, name_words(leaf_text, settings, today))
    if item.depth > 1:  # the parent folders may carry the program name
        name_score = max(
            name_score, PATH_FACTOR * similarity(query, name_words(context, settings, today))
        )

    flags: set[str] = set()
    notes: list[str] = []
    raw = (
        name_score
        + _date_adjust(req, context, settings, today, flags, notes)
        + _language_adjust(req, context, settings, flags, notes)
        + _part_adjust(req, context, flags, notes)
        + _format_adjust(req, item, flags, notes)
    )
    return Candidate(
        item, max(0.0, min(100.0, raw)), raw, name_score, tuple(notes), frozenset(flags)
    )


def nested(a: IndexedItem, b: IndexedItem) -> bool:
    """One is inside the other: they are not alternatives, just a folder and its content."""
    if a.root != b.root:
        return False
    return a.rel_path.startswith(b.rel_path + "/") or b.rel_path.startswith(a.rel_path + "/")


def rank_candidates(
    req: RequestItem,
    items: list[IndexedItem],
    settings: ParsingSettings,
    *,
    today: date,
    limit: int = 5,
) -> list[Candidate]:
    """Best distinct matches first. A folder and the files inside it are not shown together."""
    scored = [score_item(req, item, settings, today=today) for item in items]
    scored = [c for c in scored if c.score >= MIN_SHOWN]
    scored.sort(key=lambda c: (-c.raw_score, c.item.depth, c.item.name.casefold()))
    chosen: list[Candidate] = []
    for cand in scored:
        if any(nested(cand.item, other.item) for other in chosen):
            continue
        chosen.append(cand)
        if len(chosen) == limit:
            break
    return chosen

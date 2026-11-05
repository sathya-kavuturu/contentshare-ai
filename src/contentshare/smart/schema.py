"""What the model may answer, and the checks every answer goes through.

The model is asked for JSON in a fixed shape and the program enforces that shape again here:
anything that does not fit is thrown away. A name is kept only if it really appears in the
email, which stops invented names and anything an email tries to make the model say.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from contentshare.matching.exact import without_leading_date

MAX_NAMES = 12
MAX_NAME_CHARS = 150

# The shape the model is held to (llama.cpp turns this into a grammar, so it cannot answer
# in any other form).
ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_request": {"type": "boolean"},
        "confidence": {"type": "number"},
        "folders": {
            "type": "array",
            "maxItems": MAX_NAMES,
            "items": {"type": "string", "maxLength": MAX_NAME_CHARS},
        },
        "reason": {"type": "string", "maxLength": 200},
    },
    "required": ["is_request", "confidence", "folders", "reason"],
    "additionalProperties": False,
}

_SPACES = re.compile(r"\s+")
_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f<>|*?\"]|\.\.|[\\/]")  # a name is not a path or a pattern


@dataclass(frozen=True)
class Reading:
    """What was understood from one email."""

    is_request: bool
    folders: tuple[str, ...] = ()
    confidence: float = 0.0
    reason: str = ""
    dropped: tuple[str, ...] = field(default=(), compare=False)  # proposed but not in the email
    seconds: float = field(default=0.0, compare=False)  # how long the model took (0: not asked)
    answer: str = field(default="", compare=False)  # the model's own answer, as it sent it


def reading_summary(reading: Reading) -> str:
    """One line for the History: what the model answered, and how long it took."""
    verdict = "a content request" if reading.is_request else "not a content request"
    names = "; ".join(reading.folders) or "none"
    text = (
        f"Local model: {verdict}; program or folder names: {names}; "
        f"confidence {reading.confidence:.0%}"
    )
    if reading.seconds > 0:
        text += f"; answered in {reading.seconds:.1f} s"
    return text


def normalise(text: str) -> str:
    return _SPACES.sub(" ", text).strip().casefold()


def clean_name(text: str) -> str:
    name = _SPACES.sub(" ", "".join(c for c in text if c.isprintable())).strip(" .,;:-•*")
    return name[:MAX_NAME_CHARS]


def acceptable_name(name: str) -> bool:
    return len(name) >= 3 and not _BAD_CHARS.search(name) and not name.isdigit()


def parse_answer(text: str, email_text: str) -> Reading | None:
    """Check the model's reply. ``None`` when it is not the agreed JSON at all."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("is_request"), bool):
        return None
    raw_names = data.get("folders")
    if not isinstance(raw_names, list):
        raw_names = []
    haystack = normalise(email_text)
    kept: list[str] = []
    dropped: list[str] = []
    for raw in raw_names[: MAX_NAMES * 2]:
        if not isinstance(raw, str):
            continue
        if not acceptable_name(_SPACES.sub(" ", raw).strip()):
            continue  # judged as written: stripping punctuation must not hide "*.mp4" or "a/b"
        name = clean_name(raw)
        if not acceptable_name(name):
            continue
        if normalise(name) not in haystack:
            dropped.append(name)  # not written in the email: invented, or put there by the email
            continue
        name = (
            without_leading_date(name) or name
        )  # "Nov 22-28 Amber Step 4" is the folder "Amber Step 4"
        if name.casefold() not in {k.casefold() for k in kept}:
            kept.append(name)
        if len(kept) >= MAX_NAMES:
            break
    confidence = data.get("confidence")
    score = float(confidence) if isinstance(confidence, int | float) else 0.0
    reason = data.get("reason")
    return Reading(
        is_request=bool(data["is_request"]),  # by the context; names may be missing
        folders=tuple(kept),
        confidence=max(0.0, min(1.0, score)),
        reason=_SPACES.sub(" ", reason).strip()[:200] if isinstance(reason, str) else "",
        dropped=tuple(dropped),
    )

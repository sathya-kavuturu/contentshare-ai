"""Language, part/episode, file-format and filler-word detection. Shared by the email parser
(reading a request) and the matcher (reading a folder name), so both sides agree."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from functools import lru_cache

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}  # fmt: skip
_PART = re.compile(
    r"\b(part|pt|episode|ep|day|session|chapter|volume|vol|class|module)\.?\s*[-#:]?\s*"
    r"(\d{1,3}|" + "|".join(_NUMBER_WORDS) + r")\b",
    re.IGNORECASE,
)
_PART_KIND = {"pt": "part", "ep": "episode", "vol": "volume"}

FORMATS: dict[str, frozenset[str]] = {
    "video": frozenset({"mp4", "mov", "mxf", "avi", "mkv", "mts", "m4v", "video", "videos"}),
    "audio": frozenset({"mp3", "wav", "aac", "m4a", "flac", "audio"}),
    "subtitles": frozenset({"srt", "vtt", "subtitle", "subtitles", "captions"}),
    "photos": frozenset({"jpg", "jpeg", "png", "tif", "tiff", "photo", "photos", "images"}),
    "documents": frozenset({"pdf", "doc", "docx", "ppt", "pptx", "document", "documents"}),
}
_FORMAT_WORD = {word: cat for cat, words in FORMATS.items() for word in words}

STOP_WORDS = frozenset(
    """hi hello hey dear namaskaram namaste pranam pranams sir madam sirs greetings please pls
kindly share sharing shared send sending sent need needed needs require required
requires request requests requesting requested want wanted
provide give upload forward arrange help regards thanks thank you thx urgent urgently asap
immediately i we us me my our your is are was were be been am will would could can should may
might like to for of the a an and or on in at by with from this that these those it as kind
well also only just still same following below above attached mail email regarding regard re
fwd fw subject about into per recording recordings recorded video videos footage content
contents file files clip clips raw copy copies link links folder folders material materials
data details detail""".split()  # noqa: SIM905 - a long word list reads better as text
)
_STOP_PHRASES = (
    "thank you", "thanks and regards", "good morning", "good afternoon", "good evening",
    "as soon as possible", "at the earliest", "let me know", "do the needful",
)  # fmt: skip
_GREETING = re.compile(
    r"\b(?:good\s+(?:morning|afternoon|evening)|hi|hello|hey|dear|greetings)\b"
    r"(?:\s+(?:team|all|everyone|sir|sirs|madam))?",
    re.IGNORECASE,
)  # "Good morning team": the greeting and who it is addressed to, not "Morning Satsang"
_EDGE = ".-'*#" + chr(0x2018) + chr(0x2019)  # punctuation trimmed from word edges
_TOKEN = re.compile(r"[^\s,;:!?()\[\]\"“”]+")


# ------------------------------------------------------------------ languages
@lru_cache(maxsize=8)
def _language_patterns(
    key: tuple[tuple[str, tuple[str, ...]], ...],
) -> list[tuple[str, re.Pattern[str], re.Pattern[str] | None, re.Pattern[str] | None]]:
    out = []
    for name, aliases in key:
        longs = [name] + [a for a in aliases if len(a) > 3]
        codes = [a for a in aliases if len(a) <= 3]
        word = re.compile(r"\b(?:" + "|".join(map(re.escape, longs)) + r")\b", re.IGNORECASE)
        lenient = strict = None
        if codes:
            alt = "|".join(map(re.escape, codes))
            lenient = re.compile(rf"\b(?:{alt})\b", re.IGNORECASE)
            strict = re.compile(rf"\b(?:{alt.upper()})\b")  # EN, HI: not the word "hi"
        out.append((name, word, lenient, strict))
    return out


def find_languages(
    text: str, languages: Mapping[str, Sequence[str]], *, strict_codes: bool
) -> list[tuple[str, tuple[int, int]]]:
    """Languages named in ``text``. In emails (``strict_codes=True``) a short code such as
    ``HI`` counts only in capitals, so a greeting "Hi" is not read as Hindi. In folder names,
    where codes are used on purpose, any case counts."""
    text = text.replace("_", " ")  # same length, so spans stay valid
    key = tuple((n, tuple(a)) for n, a in sorted(languages.items()))
    found: list[tuple[str, tuple[int, int]]] = []
    for name, word, lenient, strict in _language_patterns(key):
        patterns = [word, strict if strict_codes else lenient]
        for pattern in patterns:
            if pattern and (m := pattern.search(text)):
                found.append((name, m.span()))
                break
    return sorted(found, key=lambda f: f[1])


# ------------------------------------------------------------------ parts / episodes
def find_parts(text: str) -> list[tuple[str, int, tuple[int, int]]]:
    """``Part 2``, ``Day1``, ``ep_05`` -> ``("part", 2, span)``, ``("day", 1, span)``..."""
    out = []
    for m in _PART.finditer(text.replace("_", " ")):
        kind = m[1].lower()
        raw = m[2].lower()
        out.append((_PART_KIND.get(kind, kind), _NUMBER_WORDS.get(raw) or int(raw), m.span()))
    return out


# ------------------------------------------------------------------ formats
def find_formats(text: str) -> list[tuple[str, tuple[int, int]]]:
    out = []
    for m in re.finditer(r"[A-Za-z0-9]+", text):
        if (cat := _FORMAT_WORD.get(m[0].lower())) is not None:
            out.append((cat, m.span()))
    return out


def format_of_extension(filename: str) -> str | None:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _FORMAT_WORD.get(ext)


# ------------------------------------------------------------------ filler words
def program_words(text: str, extra_stop: Iterable[str] = ()) -> str:
    """What is left of a request line once greetings, request verbs and filler are removed."""
    stop = STOP_WORDS | {w.casefold() for w in extra_stop}
    text = _GREETING.sub(" ", text)
    for phrase in _STOP_PHRASES:
        text = re.sub(rf"\b{re.escape(phrase)}\b", " ", text, flags=re.IGNORECASE)
    kept = []
    for token in _TOKEN.findall(text):
        bare = token.strip(_EDGE).casefold()
        if bare and bare not in stop and any(c.isalnum() for c in bare):
            kept.append(token.strip(_EDGE + ","))
    return " ".join(k for k in kept if k)

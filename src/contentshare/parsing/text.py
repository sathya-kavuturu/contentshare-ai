"""Cleaning of untrusted email text. Plain text in, plain text out: no HTML, no links followed."""

from __future__ import annotations

import html
import re
import unicodedata

MAX_BODY_CHARS = 20_000
MAX_LINE_CHARS = 500

_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_EMAIL = re.compile(r"\S+@\S+\.\S+")
_SUBJECT_PREFIX = re.compile(r"^\s*(?:(?:re|fwd?|aw)\s*:|\[[^\]]{1,30}\])\s*", re.IGNORECASE)
_QUOTE_HEADERS = (
    re.compile(r"^\s*-{2,}\s*(?:original|forwarded) message", re.IGNORECASE),
    re.compile(r"^\s*_{5,}\s*$"),
    re.compile(r"^\s*on .{5,200} wrote:\s*$", re.IGNORECASE),
    re.compile(r"^\s*from:\s.*@", re.IGNORECASE),
    re.compile(r"^\s*sent from my\b", re.IGNORECASE),
    re.compile(r"^\s*--\s*$"),
)
_SIGN_OFF = re.compile(
    r"^\s*(?:regards|thanks|thank you|warm regards|kind regards|best regards|with regards|"
    r"best wishes|pranams|namaskaram|many thanks)\b[\s,.!]*$",
    re.IGNORECASE,
)
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


_HTML_HIDDEN = re.compile(r"<(script|style)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_HTML_BREAK = re.compile(r"<\s*(?:br|/p|/div|/li|/tr|/h[1-6])\b[^>]{0,200}>", re.IGNORECASE)
_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]{0,200}>")


def strip_html(text: str) -> str:
    """Reduce HTML to its visible words. Nothing is rendered, fetched or run: tags are simply
    removed, script/style content is dropped, and line breaks are kept."""
    if not _HTML_TAG.search(text):
        return text
    text = _HTML_HIDDEN.sub(" ", text)
    text = _HTML_BREAK.sub("\n", text)
    return html.unescape(_HTML_TAG.sub(" ", text))


def strip_links(text: str) -> str:
    return _EMAIL.sub(" ", _URL.sub(" ", text))


def clean_subject(subject: str) -> str:
    text = subject.replace("\r", " ").replace("\n", " ")
    while True:
        stripped = _SUBJECT_PREFIX.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped
    return re.sub(r"\s+", " ", text).strip()[:300]


def clean_body(body: str) -> str:
    """Keep only what the sender wrote: drop quoted replies, signatures, links and addresses."""
    lines: list[str] = []
    for raw in strip_html(body[:MAX_BODY_CHARS]).replace("\r", "").split("\n"):
        line = raw[:MAX_LINE_CHARS]
        if line.lstrip().startswith(">"):
            continue
        if any(p.match(line) for p in _QUOTE_HEADERS):
            break
        if lines and _SIGN_OFF.match(line):
            break
        line = strip_links(line)
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def split_camel(text: str) -> str:
    return _CAMEL.sub(" ", text)


def normalize_words(text: str) -> str:
    """Lower-case words with single spaces: ``SunriseTalk_2026-10`` -> ``sunrise talk 2026 10``."""
    text = strip_accents(split_camel(text)).casefold()
    return " ".join(re.findall(r"[^\W_]+", text))

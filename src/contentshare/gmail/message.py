"""Turning a Gmail API message into plain data. Pure: no network, no database.

Only text is read. Attachments are never opened, HTML is never rendered (it is reduced to its
words by ``parsing.text.strip_html``), and sizes and nesting are limited so a hostile message
cannot make us do unbounded work.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.header import decode_header, make_header
from email.utils import getaddresses
from typing import Any

from contentshare.gmail.errors import MessageFormatError
from contentshare.parsing.text import strip_html

MAX_PART_BYTES = 1_000_000
MAX_PARTS = 50
MAX_DEPTH = 10
MAX_NAME_CHARS = 80
_ADDRESS = re.compile(r"^[A-Za-z0-9._%+'\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_CHARSET = re.compile(r"charset=\"?([\w.\-]+)\"?", re.IGNORECASE)


@dataclass(frozen=True)
class InboundEmail:
    message_id: str  # Gmail's id
    thread_id: str
    sender_email: str
    sender_name: str
    subject: str
    body: str
    received_at: str  # ISO 8601 UTC, from Gmail's own receive time (not the sender's Date header)
    rfc_message_id: str | None
    references: str | None
    auth_results: tuple[str, ...]  # Authentication-Results headers, top to bottom
    label_ids: tuple[str, ...]
    bulk_signal: str | None = None  # why this looks like a mass or automatic mailing, if it does

    @property
    def sender_domain(self) -> str:
        return self.sender_email.rpartition("@")[2]


def decode_header_text(value: str) -> str:
    """Decode RFC 2047 words (``=?UTF-8?B?...?=``) and remove control characters."""
    try:
        text = str(make_header(decode_header(value)))
    except (ValueError, LookupError, UnicodeError):
        text = value
    return re.sub(r"\s+", " ", _CONTROL.sub(" ", text)).strip()


def sanitize_display_name(name: str) -> str:
    """A person's name taken from a header, safe to put in a greeting."""
    text = decode_header_text(name).strip("\"'<>() ")
    return text[:MAX_NAME_CHARS]


def parse_sender(from_header: str) -> tuple[str, str]:
    """``(address, display name)`` from a From header. Rejects anything but exactly one plain
    address, so a crafted header cannot smuggle in a second recipient or odd syntax."""
    people = [(n, a) for n, a in getaddresses([from_header]) if a]
    if len(people) != 1:
        raise MessageFormatError("From header does not contain exactly one address")
    name, address = people[0]
    address = address.strip().lower()
    if not _ADDRESS.match(address) or ".." in address:
        raise MessageFormatError("From address is not usable")
    return address, sanitize_display_name(name)


def _headers(payload: dict[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for header in payload.get("headers", []):
        out.setdefault(str(header.get("name", "")).lower(), []).append(str(header.get("value", "")))
    return out


def _decode_body(data: str, content_type: str) -> str:
    if len(data) > MAX_PART_BYTES * 4 // 3 + 4:
        data = data[: MAX_PART_BYTES * 4 // 3]
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        return ""
    match = _CHARSET.search(content_type)
    try:
        return raw[:MAX_PART_BYTES].decode(match.group(1) if match else "utf-8", errors="replace")
    except LookupError:
        return raw[:MAX_PART_BYTES].decode("utf-8", errors="replace")


def extract_text(payload: dict[str, Any]) -> str:
    """The readable text of a message: its text/plain parts, or failing that its HTML reduced
    to words. Attachments and anything beyond the limits are skipped."""
    plain: list[str] = []
    html: list[str] = []
    budget = [MAX_PARTS]

    def walk(part: dict[str, Any], depth: int) -> None:
        if depth > MAX_DEPTH or budget[0] <= 0:
            return
        budget[0] -= 1
        body = part.get("body", {})
        if part.get("filename") or body.get("attachmentId"):
            return  # an attachment: never opened
        mime = str(part.get("mimeType", "")).lower()
        children = part.get("parts")
        if children:
            for child in children:
                walk(child, depth + 1)
            return
        data = body.get("data")
        if not data:
            return
        content_type = next(
            (
                h.get("value", "")
                for h in part.get("headers", [])
                if h.get("name", "").lower() == "content-type"
            ),
            "",
        )
        text = _decode_body(data, content_type)
        if mime == "text/plain":
            plain.append(text)
        elif mime == "text/html":
            html.append(text)

    walk(payload, 0)
    if plain:
        return "\n".join(plain)
    return strip_html("\n".join(html))


def bulk_signal(headers: dict[str, list[str]]) -> str | None:
    """Headers that mark a mass mailing or an automatic message (newsletters, notifications).
    People writing to ask for something do not send these."""
    if "list-unsubscribe" in headers:
        return "mass mailing (it has an unsubscribe link)"
    precedence = headers.get("precedence", [""])[0].strip().lower()
    if precedence in ("bulk", "list", "junk"):
        return "mass mailing (marked as bulk mail)"
    automatic = headers.get("auto-submitted", [""])[0].strip().lower()
    if automatic and automatic != "no":
        return "automatic message"
    return None


def parse_message(data: dict[str, Any]) -> InboundEmail:
    """Build an :class:`InboundEmail` from a ``users.messages.get(format="full")`` result."""
    try:
        message_id, thread_id = str(data["id"]), str(data["threadId"])
        payload = data["payload"]
    except KeyError as exc:
        raise MessageFormatError(f"Message is missing {exc.args[0]}") from exc
    headers = _headers(payload)
    if "from" not in headers:
        raise MessageFormatError("Message has no From header")
    sender_email, sender_name = parse_sender(headers["from"][0])
    try:
        received = datetime.fromtimestamp(int(data["internalDate"]) / 1000, tz=UTC)
    except (KeyError, ValueError, OSError, OverflowError):
        received = datetime.now(UTC)
    rfc_id = headers.get("message-id", [None])[0]
    return InboundEmail(
        message_id=message_id,
        thread_id=thread_id,
        sender_email=sender_email,
        sender_name=sender_name,
        subject=decode_header_text(headers.get("subject", [""])[0])[:300],
        body=extract_text(payload),
        received_at=received.isoformat(timespec="seconds"),
        rfc_message_id=rfc_id.strip() if rfc_id else None,
        references=" ".join(headers.get("references", [])).strip() or None,
        auth_results=tuple(headers.get("authentication-results", [])),
        label_ids=tuple(data.get("labelIds", [])),
        bulk_signal=bulk_signal(headers),
    )

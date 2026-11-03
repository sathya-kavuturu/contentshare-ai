"""Building the reply email. Pure: no network, no database.

Every value that came from the original email is checked before it goes into a header, and the
standard library's strict policy refuses any header containing a line break, so a crafted
subject or name cannot add headers or recipients. The reply goes to the original sender's From
address only, never to a Reply-To or anywhere else.
"""

from __future__ import annotations

import base64
import re
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import make_msgid

from contentshare.parsing.text import clean_subject

MAX_BODY_CHARS = 20_000
REPLY_TAG_HEADER = "X-ContentShare-Reply"
_MSG_ID = re.compile(r"^<[^<>\s@]{1,200}@[^<>\s@]{1,200}>$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAX_REFERENCES = 20


def reply_subject(original: str) -> str:
    base = clean_subject(original)
    return f"Re: {base}" if base else "Re: your request"


def valid_message_id(value: str | None) -> bool:
    return bool(value and _MSG_ID.match(value.strip()))


def threading_headers(original_id: str | None, original_references: str | None) -> dict[str, str]:
    """``In-Reply-To`` and ``References`` so mail clients show the reply in the same thread.
    Anything that is not a well-formed message id is dropped."""
    if original_id is None or not valid_message_id(original_id):
        return {}
    parent = original_id.strip()
    earlier = [r for r in (original_references or "").split() if _MSG_ID.match(r)]
    chain = [*earlier[-(MAX_REFERENCES - 1) :], parent]
    return {"In-Reply-To": parent, "References": " ".join(chain)}


def clean_body_text(body: str) -> str:
    text = _CONTROL.sub("", body.replace("\r\n", "\n").replace("\r", "\n"))
    return text[:MAX_BODY_CHARS].strip() + "\n"


def build_reply(
    *,
    to_addr: str,
    subject: str,
    body: str,
    token: str,
    original_message_id: str | None,
    original_references: str | None,
) -> EmailMessage:
    msg = EmailMessage(policy=SMTP)
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain="contentshare.local")
    for name, value in threading_headers(original_message_id, original_references).items():
        msg[name] = value
    msg[REPLY_TAG_HEADER] = token  # lets us find this reply in Gmail if the app dies mid-send
    msg.set_content(clean_body_text(body), charset="utf-8")
    return msg


def encode_raw(msg: EmailMessage) -> str:
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

"""Cheap, certain judgements about a mail, made before any model is asked: which Gmail label
group it is in, and whether it is plainly an automatic mailing. Pure: no network, no database."""

from __future__ import annotations

import re
from collections.abc import Iterable

from contentshare.gmail.message import InboundEmail

CATEGORIES = ("Primary", "Promotions", "Social", "Updates", "Forums", "Spam", "Other")
_CATEGORY_OF = {
    "CATEGORY_PROMOTIONS": "Promotions",
    "CATEGORY_SOCIAL": "Social",
    "CATEGORY_UPDATES": "Updates",
    "CATEGORY_FORUMS": "Forums",
}
_AUTOMATIC_LOCAL = re.compile(
    r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|mailer-daemon|postmaster|notifications?|bounce[sd]?)"
    r"([-_.+].*)?$"
)


def mail_category(labels: Iterable[str]) -> str:
    """The group Gmail files a mail under, from its label ids ("Primary" is the main inbox)."""
    ids = set(labels)
    if "SPAM" in ids:
        return "Spam"
    for label, name in _CATEGORY_OF.items():
        if label in ids:
            return name
    return "Primary" if "INBOX" in ids else "Other"


def automatic_sender(address: str) -> bool:
    """``noreply@…``, ``mailer-daemon@…`` and the like: nobody reads the answer."""
    return bool(_AUTOMATIC_LOCAL.match(address.partition("@")[0].lower()))


def plainly_automatic(msg: InboundEmail) -> str | None:
    """Why this mail is certainly not a person's request, or ``None`` when it may be one."""
    if msg.bulk_signal:
        return msg.bulk_signal
    if automatic_sender(msg.sender_email):
        return "sent by an automatic address (no-reply)"
    return None

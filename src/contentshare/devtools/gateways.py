"""Stand-ins for the Gmail gateway, used only in developer mode.

``FakeGateway`` is a mailbox of canned test emails: nothing real is read or sent. ``NoSendGateway``
wraps the real gateway and refuses only to send, so mail can be read and checked while testing
without any reply leaving."""

from __future__ import annotations

import base64
import logging
from collections.abc import Sequence
from typing import Any

log = logging.getLogger(__name__)

_AUTH = (
    "mx.google.com; dkim=pass header.i=@{d}; spf=pass smtp.mailfrom=x@{d}; "
    "dmarc=pass header.from={d}"
)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _message(
    message_id: str, sender: str, subject: str, body: str, labels: Sequence[str] = ("INBOX",)
) -> dict[str, Any]:
    domain = sender.rpartition("@")[2].rstrip(">")
    headers = [
        {"name": "From", "value": sender},
        {"name": "Subject", "value": subject},
        {"name": "Message-ID", "value": f"<{message_id}@test.invalid>"},
        {"name": "Authentication-Results", "value": _AUTH.format(d=domain)},
    ]
    return {
        "id": message_id,
        "threadId": f"thread-{message_id}",
        "labelIds": list(labels),
        "internalDate": "1793000000000",
        "payload": {
            "mimeType": "text/plain",
            "headers": headers,
            "body": {"data": _b64(body)},
        },
    }


# One of each kind of mail the program has to tell apart. The department sender is the first
# department's first sender (filled in by the caller), the rest are strangers.
def canned(department_sender: str | None) -> list[dict[str, Any]]:
    mails = [
        _message(
            "dev-stranger-polite",
            "Test Person <someone@example.test>",
            "Request for a program",
            'Hello, we would be very grateful if you could kindly share the "Online Isha Janani '
            'Sessions AV KIT v001" session with us. Thank you for all the seva you do.',
        ),
        _message(
            "dev-stranger-no-name",
            "Another Person <another@example.test>",
            "Content please",
            "Could you please send us the recording of last weekend's satsang?",
        ),
        _message(
            "dev-newsletter",
            "Weekly News <editor@news.example.test>",
            "Your weekly newsletter",
            "Here is what happened this week. Read more on our website.",
        ),
        _message(
            "dev-greeting",
            "Friend <friend@example.test>",
            "Hello",
            "Hope you are well. Just saying hello!",
        ),
    ]
    if department_sender:
        mails.insert(
            0,
            _message(
                "dev-department",
                f"Department Person <{department_sender}>",
                "Need a recording",
                "Please share the folder named Test Program 2026 and send it to us. Thanks.",
            ),
        )
    return mails


class FakeGateway:
    """Same methods as ``GmailGateway``; the mailbox is a fixed list of test emails."""

    def __init__(self, department_sender: str | None = None) -> None:
        self._messages = {m["id"]: m for m in canned(department_sender)}
        self.sent: list[str] = []
        self._labelled: set[str] = set()

    def profile_email(self) -> str:
        return "fake-mailbox@test.invalid"

    def list_message_ids(
        self, query: str, limit: int, *, include_spam_trash: bool = False
    ) -> list[str]:
        log.info("fake gmail: search %r", query)
        return [i for i in self._messages if i not in self._labelled][:limit]

    def get_message(self, message_id: str) -> dict[str, Any]:
        return dict(self._messages[message_id])

    def thread_headers(self, thread_id: str, names: Sequence[str]) -> list[dict[str, Any]]:
        return []

    def ensure_label(self, name: str) -> str:
        return f"fake-label-{name}"

    def add_label(self, message_id: str, label_id: str) -> None:
        self._labelled.add(message_id)

    def send_raw(self, raw_base64url: str, thread_id: str | None) -> str:
        log.warning("fake gmail: a reply was 'sent' to the test mailbox only")
        self.sent.append(raw_base64url)
        return f"fake-sent-{len(self.sent)}"


class NoSendGateway:
    """Reads from the real mailbox; sending is turned off."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def send_raw(self, raw_base64url: str, thread_id: str | None) -> str:
        log.warning("developer mode: a reply was NOT sent (sending is switched off)")
        return "dev-not-sent"

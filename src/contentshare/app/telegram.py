"""Telegram alerts: one short message to a group when new requests wait for approval.

Only counts are ever sent, never an email subject, sender, program name or path (the same rule
as the pop-ups): a Telegram group can have many members and keeps its history on other servers.

The bot token is a password. It lives only in Windows Credential Manager (via ``keyring``), never
in the settings file, never in a log line, and never in an error message: a ``requests`` error
text contains the address it called, and that address contains the token, so errors are reported
by what went wrong, not by their own text.
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import keyring
import requests
from keyring.errors import PasswordDeleteError

from contentshare.config.models import TELEGRAM_CHAT
from contentshare.gmail.intake import IntakeReport

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
TIMEOUT_S = 10.0
# What @BotFather hands out: digits, a colon, then letters, digits, "_" and "-".
_TOKEN = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,60}$")
# A group or chat number (groups are negative), or @name of a public channel.

HttpPost = Callable[[str, dict[str, Any], float], tuple[int, dict[str, Any]]]


class TelegramError(Exception):
    """A readable reason, safe to show: it never contains the token."""


def valid_token(value: str) -> bool:
    return bool(_TOKEN.match(value))


def valid_chat(value: str) -> bool:
    return bool(TELEGRAM_CHAT.match(value))


def alert_text(report: IntakeReport) -> str | None:
    """The group message when a check found new requests: counts only, nothing from the emails
    (a group has many readers and keeps its history on Telegram's servers)."""
    if not report.requests:
        return None
    n = report.requests
    counts = (
        (report.ready, f"{report.ready} ready"),
        (report.needs_review, f"{report.needs_review} need a look"),
    )
    detail = ", ".join(text for count, text in counts if count)
    return (
        f"ContentShare: {n} new request{'' if n == 1 else 's'} waiting for approval"
        + (f" ({detail})" if detail else "")
        + ". Open ContentShare to review and approve. Nothing has been copied or sent."
    )


class SecretStore(Protocol):
    def get(self) -> str | None: ...
    def set(self, value: str) -> None: ...
    def clear(self) -> None: ...


class KeyringSecret:
    """The bot token in Windows Credential Manager."""

    def __init__(self, service: str = "ContentShare", name: str = "telegram-bot-token") -> None:
        self.service, self.name = service, name

    def get(self) -> str | None:
        return keyring.get_password(self.service, self.name)

    def set(self, value: str) -> None:
        keyring.set_password(self.service, self.name, value)

    def clear(self) -> None:
        with contextlib.suppress(PasswordDeleteError):
            keyring.delete_password(self.service, self.name)


def _post_json(url: str, body: dict[str, Any], timeout: float) -> tuple[int, dict[str, Any]]:
    # No redirects: the address carries the token and must only ever go to Telegram.
    response = requests.post(url, json=body, timeout=timeout, allow_redirects=False)
    try:
        data = response.json()
    except ValueError:
        data = {}
    return response.status_code, data if isinstance(data, dict) else {}


_STATUS_TEXT = {
    401: "Telegram did not accept the bot token. Check it, or make a new one with @BotFather.",
    403: "The bot is not allowed to post there. Add the bot to the group again.",
    404: "Telegram did not accept the bot token. Check it, or make a new one with @BotFather.",
    429: "Telegram asked for a pause. The next alert will go through.",
}


@dataclass(frozen=True)
class Chat:
    chat_id: str
    title: str


class TelegramNotifier:
    def __init__(self, store: SecretStore | None = None, post: HttpPost | None = None) -> None:
        self._store = store or KeyringSecret()
        self._post = post or _post_json

    # --------------------------------------------------------------- the token
    def has_token(self) -> bool:
        return bool(self._store.get())

    def save_token(self, token: str) -> None:
        token = token.strip()
        if not valid_token(token):
            raise TelegramError(
                "That does not look like a bot token. It is the long text from @BotFather, "
                "like 123456789:AA... (digits, a colon, then letters and digits)."
            )
        self._store.set(token)

    def forget_token(self) -> None:
        self._store.clear()

    # --------------------------------------------------------------- calls
    def _call(self, token: str, method: str, body: dict[str, Any]) -> dict[str, Any]:
        if not valid_token(token):
            raise TelegramError("There is no valid bot token. Paste the one from @BotFather.")
        try:
            status, data = self._post(f"{API}/bot{token}/{method}", body, TIMEOUT_S)
        except requests.RequestException as exc:
            # Never show str(exc): it contains the address, and the address contains the token.
            raise TelegramError(
                f"Telegram could not be reached ({type(exc).__name__}). Check the internet "
                "connection."
            ) from None
        if status == 200 and data.get("ok") is True:
            return data
        if status in _STATUS_TEXT:
            raise TelegramError(_STATUS_TEXT[status])
        if status == 400:
            raise TelegramError(
                "Telegram could not find that chat. Add the bot to the group, then use "
                "“Find my group” to fill in the number."
            )
        raise TelegramError(f"Telegram answered with error {status}.")

    def send(self, chat_id: str, text: str, *, token: str | None = None) -> None:
        """Post ``text`` to ``chat_id``. Raises :class:`TelegramError` with a safe reason."""
        chat_id = chat_id.strip()
        if not valid_chat(chat_id):
            raise TelegramError(
                "The chat number is not valid. A group number looks like -1001234567890."
            )
        self._call(
            token or self._store.get() or "",
            "sendMessage",
            {"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        )

    def recent_chats(self, *, token: str | None = None) -> list[Chat]:
        """Groups the bot has recently seen (someone wrote in the group after the bot joined).
        Only the group number and name are read; messages are not kept."""
        data = self._call(token or self._store.get() or "", "getUpdates", {"limit": 100})
        found: dict[str, Chat] = {}
        for update in data.get("result", []):
            if not isinstance(update, dict):
                continue
            for key in ("message", "channel_post", "my_chat_member"):
                chat = (update.get(key) or {}).get("chat")
                if isinstance(chat, dict) and chat.get("type") in (
                    "group",
                    "supergroup",
                    "channel",
                ):
                    chat_id = str(chat.get("id"))
                    found[chat_id] = Chat(chat_id, str(chat.get("title") or chat_id))
        return list(found.values())

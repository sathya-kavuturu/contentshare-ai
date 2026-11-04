"""Telegram alerts: what is sent, what is never sent, and that the token never leaks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import requests

from contentshare.app.controller import Controller, Event
from contentshare.app.telegram import (
    Chat,
    TelegramError,
    TelegramNotifier,
    alert_text,
    valid_chat,
    valid_token,
)
from contentshare.config.models import AppConfig, TelegramSettings
from contentshare.gmail.intake import IntakeReport
from contentshare.storage.db import Database
from fake_gmail import make_message
from helpers import TODAY, build_mail_world

TOKEN = "123456789:AAH-abcdefghijklmnopqrstuvwxyz012345"
GROUP = "-1001234567890"
SUBJECT = "Sunrise Talk 12 Oct 2026"


class Store:
    def __init__(self, value: str | None = None) -> None:
        self.value = value

    def get(self) -> str | None:
        return self.value

    def set(self, value: str) -> None:
        self.value = value

    def clear(self) -> None:
        self.value = None


class Wire:
    """Stands in for the internet: records calls, answers as told."""

    def __init__(self, status: int = 200, body: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any], float]] = []
        self.status, self.body = status, body if body is not None else {"ok": True}
        self.raises: Exception | None = None

    def __call__(
        self, url: str, body: dict[str, Any], timeout: float
    ) -> tuple[int, dict[str, Any]]:
        self.calls.append((url, body, timeout))
        if self.raises:
            raise self.raises
        return self.status, self.body


def notifier(wire: Wire, token: str | None = TOKEN) -> TelegramNotifier:
    return TelegramNotifier(Store(token), wire)


# --------------------------------------------------------------------------- the notifier
def test_send_posts_to_the_bot_api_with_the_stored_token() -> None:
    wire = Wire()
    notifier(wire).send(GROUP, "hello")
    ((url, body, timeout),) = wire.calls
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert body["chat_id"] == GROUP and body["text"] == "hello"
    assert 0 < timeout <= 15


def test_a_typed_token_is_used_before_one_is_saved() -> None:
    wire = Wire()
    notifier(wire, token=None).send(GROUP, "x", token=TOKEN)
    assert TOKEN in wire.calls[0][0]


@pytest.mark.parametrize(
    "bad", ["", "abc", "123:short", "12345 6789:AAH-abcdefghijklmnopqrstuvwxyz0123"]
)
def test_a_bad_token_is_refused_before_any_call(bad: str) -> None:
    wire = Wire()
    with pytest.raises(TelegramError):
        notifier(wire, token=None).send(GROUP, "x", token=bad)
    assert wire.calls == [] and not valid_token(bad)


@pytest.mark.parametrize(
    "bad", ["", "group", "-100 123", "../x", "@ab", "-1001234567890123456789012"]
)
def test_a_bad_chat_is_refused_before_any_call(bad: str) -> None:
    wire = Wire()
    with pytest.raises(TelegramError):
        notifier(wire).send(bad, "x")
    assert wire.calls == [] and not valid_chat(bad)


@pytest.mark.parametrize("good", [GROUP, "123456", "@my_channel"])
def test_good_chats(good: str) -> None:
    assert valid_chat(good)


@pytest.mark.parametrize(
    ("status", "fragment"),
    [
        (401, "bot token"),
        (403, "not allowed"),
        (400, "find that chat"),
        (429, "pause"),
        (500, "error 500"),
    ],
)
def test_telegram_errors_become_readable_text_without_the_token(status: int, fragment: str) -> None:
    wire = Wire(status, {"ok": False, "description": f"secret {TOKEN}"})
    with pytest.raises(TelegramError) as caught:
        notifier(wire).send(GROUP, "x")
    assert fragment in str(caught.value) and TOKEN not in str(caught.value)


def test_a_network_error_never_shows_the_address() -> None:
    wire = Wire()
    wire.raises = requests.ConnectionError(f"HTTPSConnectionPool: /bot{TOKEN}/sendMessage")
    with pytest.raises(TelegramError) as caught:
        notifier(wire).send(GROUP, "x")
    assert "ConnectionError" in str(caught.value) and TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_200_without_ok_is_a_failure() -> None:
    with pytest.raises(TelegramError):
        notifier(Wire(200, {"ok": False})).send(GROUP, "x")


def test_the_token_is_saved_checked_and_forgotten() -> None:
    store = Store()
    n = TelegramNotifier(store, Wire())
    assert not n.has_token()
    with pytest.raises(TelegramError):
        n.save_token("nonsense")
    n.save_token(f"  {TOKEN}  ")
    assert n.has_token() and store.value == TOKEN
    n.forget_token()
    assert not n.has_token()


def test_recent_chats_lists_each_group_once_and_skips_private_chats() -> None:
    updates = [
        {"message": {"chat": {"id": -100, "type": "supergroup", "title": "Media"}}},
        {"message": {"chat": {"id": -100, "type": "supergroup", "title": "Media"}}},
        {"my_chat_member": {"chat": {"id": -200, "type": "group", "title": "Team"}}},
        {"message": {"chat": {"id": 55, "type": "private", "first_name": "Someone"}}},
        {"edited_message": {}},
        "junk",
    ]
    chats = notifier(Wire(200, {"ok": True, "result": updates})).recent_chats()
    assert chats == [Chat("-100", "Media"), Chat("-200", "Team")]


# --------------------------------------------------------------------------- the message
def test_the_message_has_counts_only() -> None:
    text = alert_text(IntakeReport(examined=2, requests=3, ready=2, needs_review=1))
    assert text is not None
    assert "3 new requests" in text and "2 ready" in text and "1 need a look" in text
    assert "Nothing has been copied or sent" in text
    one = alert_text(IntakeReport(requests=1, ready=1))
    assert one is not None and "1 new request waiting" in one and "need a look" not in one


def test_no_new_requests_no_message() -> None:
    assert alert_text(IntakeReport(examined=4)) is None


# --------------------------------------------------------------------------- the controller
class Fake:
    """A notifier that records what would be sent."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail: Exception | None = None

    def send(self, chat_id: str, text: str, *, token: str | None = None) -> None:
        if self.fail:
            raise self.fail
        self.sent.append((chat_id, text))


def build(
    tmp_path: Path, db: Database, *, enabled: bool = True
) -> tuple[Controller, Fake, list[Event]]:
    world = build_mail_world(tmp_path, db)
    world.config.telegram = TelegramSettings(enabled=enabled, chat_id=GROUP)
    fake = Fake()
    ctl = Controller(
        world.repo, world.index, world.config,
        gateway_factory=lambda: world.gateway,
        config_path=tmp_path / "config.yaml", today=lambda: TODAY,
        telegram=fake,  # type: ignore[arg-type]
    )  # fmt: skip
    events: list[Event] = []
    ctl.subscribe(events.append)
    ctl._world = world  # type: ignore[attr-defined]
    return ctl, fake, events


def add_mail(ctl: Controller, message_id: str = "m1") -> None:
    world = ctl._world  # type: ignore[attr-defined]
    world.gmail.add(
        make_message(
            message_id=message_id,
            subject=SUBJECT,
            body="Please share the English recording.\nThanks",
        )
    )


def test_new_requests_send_one_message_with_counts_only(tmp_path: Path, db: Database) -> None:
    ctl, fake, _ = build(tmp_path, db)
    add_mail(ctl)
    ctl.check_now()
    ((chat, text),) = fake.sent
    assert chat == GROUP and "1 new request" in text
    for private in (SUBJECT, "Sunrise", "Media", "@", "example"):
        assert private not in text
    ctl.check_now()  # nothing new now
    assert len(fake.sent) == 1
    ctl.shutdown()


def test_switched_off_or_without_a_group_nothing_is_sent(tmp_path: Path, db: Database) -> None:
    ctl, fake, _ = build(tmp_path, db, enabled=False)
    add_mail(ctl)
    ctl.check_now()
    assert fake.sent == []
    ctl.shutdown()
    (tmp_path / "again").mkdir()
    ctl, fake, _ = build(tmp_path / "again", db, enabled=True)
    ctl.config.telegram = TelegramSettings(enabled=True, chat_id="")
    add_mail(ctl, "m2")
    report = ctl.check_now()
    assert report.requests == 1 and fake.sent == []  # a new request, but no group to tell
    ctl.shutdown()


@pytest.mark.parametrize(
    "error",
    [TelegramError("The bot is not allowed to post there."), OSError("credential store locked")],
)
def test_a_failed_alert_is_reported_and_the_check_still_works(
    tmp_path: Path, db: Database, error: Exception
) -> None:
    ctl, fake, events = build(tmp_path, db)
    fake.fail = error
    add_mail(ctl)
    report = ctl.check_now()
    assert report.requests == 1 and ctl.views()  # the request is on screen regardless
    problems = [e for e in events if e.kind == "telegram_problem"]
    assert len(problems) == 1 and "credential store locked" not in problems[0].message
    ctl.shutdown()


def test_the_settings_accept_only_a_valid_chat() -> None:
    assert AppConfig().telegram.enabled is False
    with pytest.raises(ValueError, match="chat number"):
        TelegramSettings(chat_id="my group")
    assert TelegramSettings(chat_id=f" {GROUP} ").chat_id == GROUP

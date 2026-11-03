from __future__ import annotations

import base64
import email

import httplib2
import pytest

from contentshare.gmail.errors import (
    AuthRequiredError,
    GmailError,
    GmailNotFoundError,
    TransientGmailError,
)
from contentshare.gmail.gateway import GmailGateway, map_error
from fake_gmail import FakeGmail, http_error, make_message


@pytest.fixture
def gmail() -> FakeGmail:
    return FakeGmail(page_size=3)


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def gateway(gmail: FakeGmail, sleeps: list[float]) -> GmailGateway:
    return GmailGateway(gmail, sleep=sleeps.append)


def add_messages(gmail: FakeGmail, n: int) -> list[str]:
    ids = []
    for i in range(n):
        m = gmail.add(make_message(message_id=f"m{i}", internal_ms=1_793_000_000_000 + i * 1000))
        ids.append(m["id"])
    return ids


# ------------------------------------------------------------------ reading
def test_profile(gateway: GmailGateway) -> None:
    assert gateway.profile_email() == "me@org.org"


def test_listing_follows_pages_and_respects_the_limit(
    gateway: GmailGateway, gmail: FakeGmail
) -> None:
    add_messages(gmail, 8)
    ids = gateway.list_message_ids("from:(anita@dept.org)", 100)
    assert ids == [f"m{i}" for i in range(7, -1, -1)]  # newest first, across 3 pages
    assert gmail.calls.count("messages.list") == 3
    assert gateway.list_message_ids("from:(anita@dept.org)", 4) == ["m7", "m6", "m5", "m4"]
    assert gateway.list_message_ids("from:(nobody@x.org)", 10) == []


def test_get_message_full(gateway: GmailGateway, gmail: FakeGmail) -> None:
    gmail.add(make_message(message_id="abc", body="hello"))
    assert gateway.get_message("abc")["id"] == "abc"
    with pytest.raises(GmailNotFoundError):
        gateway.get_message("missing")


def test_thread_headers_returns_only_requested_headers(
    gateway: GmailGateway, gmail: FakeGmail
) -> None:
    gmail.add(make_message(message_id="a", thread_id="T", extra_headers=[("X-Tag", "1")]))
    (info,) = gateway.thread_headers("T", ["X-Tag"])
    assert info["headers"] == {"x-tag": "1"}
    assert info["labels"] == ["INBOX"]


# ------------------------------------------------------------------ labels
def test_label_is_created_once_then_reused(gateway: GmailGateway, gmail: FakeGmail) -> None:
    first = gateway.ensure_label("ContentShare/Processed")
    assert gateway.ensure_label("ContentShare/Processed") == first
    assert gmail.calls.count("labels.create") == 1
    assert gmail.calls.count("labels.list") == 1  # second call came from the cache
    other = GmailGateway(gmail)  # a new run finds the existing label instead of making another
    assert other.ensure_label("ContentShare/Processed") == first
    assert gmail.calls.count("labels.create") == 1


def test_label_creation_race_is_survived(gateway: GmailGateway, gmail: FakeGmail) -> None:
    gmail.labels["Label_9"] = "ContentShare/Processed"
    gmail.fail("labels.list", http_error(503))  # first lookup fails once, then the label exists
    assert gateway.ensure_label("ContentShare/Processed") == "Label_9"


def test_add_label(gateway: GmailGateway, gmail: FakeGmail) -> None:
    gmail.add(make_message(message_id="m1"))
    label = gateway.ensure_label("L")
    gateway.add_label("m1", label)
    gateway.add_label("m1", label)
    assert gmail.label_ids_of("m1") == ["INBOX", label]


# ------------------------------------------------------------------ sending
def test_send_posts_raw_message_into_the_thread(gateway: GmailGateway, gmail: FakeGmail) -> None:
    raw = base64.urlsafe_b64encode(b"To: a@dept.org\r\nSubject: Re: x\r\n\r\nhi\r\n").decode()
    sent_id = gateway.send_raw(raw, "thread-1")
    assert sent_id == "sent1"
    assert gmail.sent[0]["threadId"] == "thread-1"
    assert email.message_from_bytes(gmail.sent[0]["raw"])["Subject"] == "Re: x"
    gateway.send_raw(raw, None)
    assert gmail.sent[1]["threadId"].startswith("t-")


def test_send_is_never_retried_automatically(
    gateway: GmailGateway, gmail: FakeGmail, sleeps: list[float]
) -> None:
    gmail.fail("messages.send", http_error(503), http_error(503))
    with pytest.raises(TransientGmailError):
        gateway.send_raw("eA", "t")
    assert gmail.calls.count("messages.send") == 1  # a retry could send the reply twice
    assert sleeps == []


# ------------------------------------------------------------------ retries and errors
def test_transient_errors_are_retried_with_backoff(
    gateway: GmailGateway, gmail: FakeGmail, sleeps: list[float]
) -> None:
    gmail.fail(
        "getProfile", http_error(429, "rateLimitExceeded"), http_error(503), OSError("reset")
    )
    assert gateway.profile_email() == "me@org.org"
    assert sleeps == [1, 2, 4]


def test_gives_up_after_the_last_attempt(
    gateway: GmailGateway, gmail: FakeGmail, sleeps: list[float]
) -> None:
    gmail.fail("getProfile", *[http_error(500) for _ in range(10)])
    with pytest.raises(TransientGmailError):
        gateway.profile_email()
    assert gmail.calls.count("getProfile") == 4
    assert sleeps == [1, 2, 4]


@pytest.mark.parametrize(
    ("status", "reason", "kind"),
    [
        (401, "authError", AuthRequiredError),
        (403, "insufficientPermissions", AuthRequiredError),
        (403, "forbidden", AuthRequiredError),
        (403, "rateLimitExceeded", TransientGmailError),
        (403, "userRateLimitExceeded", TransientGmailError),
        (429, "", TransientGmailError),
        (500, "", TransientGmailError),
        (503, "backendError", TransientGmailError),
        (404, "notFound", GmailNotFoundError),
        (400, "invalidArgument", GmailError),
    ],
)
def test_error_mapping(status: int, reason: str, kind: type[GmailError]) -> None:
    mapped = map_error(http_error(status, reason))
    assert type(mapped) is kind


def test_auth_errors_are_not_retried(
    gateway: GmailGateway, gmail: FakeGmail, sleeps: list[float]
) -> None:
    gmail.fail("getProfile", http_error(401))
    with pytest.raises(AuthRequiredError):
        gateway.profile_email()
    assert sleeps == []


@pytest.mark.parametrize(
    "exc",
    [OSError("x"), TimeoutError(), ConnectionResetError(), httplib2.ServerNotFoundError("dns")],
)
def test_network_failures_are_transient(exc: Exception) -> None:
    assert isinstance(map_error(exc), TransientGmailError)


def test_unexpected_errors_are_wrapped_without_their_text() -> None:
    mapped = map_error(ValueError("contains the secret token abc123"))
    assert "abc123" not in str(mapped)
    assert type(mapped) is GmailError


def test_error_messages_carry_no_response_body() -> None:
    exc = http_error(
        400, "invalidArgument", message="Invalid raw: Dear Anita, your secret is hunter2"
    )
    assert "hunter2" not in str(map_error(exc))

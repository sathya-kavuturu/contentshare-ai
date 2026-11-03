from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from contentshare.gmail.errors import MessageFormatError
from contentshare.gmail.message import (
    MAX_NAME_CHARS,
    decode_header_text,
    extract_text,
    parse_message,
    parse_sender,
    sanitize_display_name,
)
from contentshare.gmail.sender_auth import Verdict, aligned, verify_sender
from fake_gmail import auth_header, b64, make_message


# ------------------------------------------------------------------ parsing a message
def test_plain_message() -> None:
    msg = parse_message(make_message(subject="Sunrise Talk 12 Oct", body="Please share"))
    assert msg.sender_email == "anita@dept.org"
    assert msg.sender_name == "Anita R."
    assert msg.sender_domain == "dept.org"
    assert msg.subject == "Sunrise Talk 12 Oct"
    assert msg.body == "Please share"
    expected = datetime.fromtimestamp(1_793_000_000, UTC).isoformat(timespec="seconds")
    assert msg.received_at == expected  # Gmail's receive time, not the sender's Date header
    assert msg.rfc_message_id.startswith("<orig")  # type: ignore[union-attr]
    assert msg.label_ids == ("INBOX",)


def test_html_only_message_is_reduced_to_text() -> None:
    raw = make_message(
        body="",
        html="<p>Please share <b>Sunrise Talk</b></p><script>alert(1)</script><style>x{}</style>",
    )
    assert "Sunrise Talk" in parse_message(raw).body
    assert "alert" not in parse_message(raw).body


def test_plain_text_part_wins_over_html() -> None:
    raw = make_message(body="plain version", html="<p>html version</p>")
    assert parse_message(raw).body == "plain version"


def test_attachments_are_never_read() -> None:
    raw = make_message(body="hello", attachments=[("secret.docx", "x" * 100), ("run.exe", "y")])
    assert parse_message(raw).body == "hello"


def test_charset_is_honoured_and_bad_data_does_not_crash() -> None:
    payload: dict[str, Any] = {
        "mimeType": "text/plain",
        "headers": [{"name": "Content-Type", "value": 'text/plain; charset="iso-8859-1"'}],
        "body": {"data": "Y2Fm6Q"},  # "caf\xe9" in latin-1
    }
    assert extract_text(payload) == "café"
    assert extract_text({"mimeType": "text/plain", "body": {"data": "!!!not base64!!!"}}) == ""
    bad_charset = {
        "mimeType": "text/plain",
        "headers": [{"name": "Content-Type", "value": "text/plain; charset=nonsense-9"}],
        "body": {"data": b64("ok")},
    }
    assert extract_text(bad_charset) == "ok"


def test_part_and_depth_limits() -> None:
    leaf = {"mimeType": "text/plain", "body": {"data": b64("deep")}}
    node: dict[str, Any] = leaf
    for _ in range(30):
        node = {"mimeType": "multipart/mixed", "parts": [node]}
    assert extract_text(node) == ""  # too deeply nested to follow

    many = {
        "mimeType": "multipart/mixed",
        "parts": [{"mimeType": "text/plain", "body": {"data": b64(f"p{i}")}} for i in range(200)],
    }
    assert len(extract_text(many).split("\n")) <= 50


def test_huge_part_is_truncated() -> None:
    payload = {"mimeType": "text/plain", "body": {"data": b64("x" * 3_000_000)}}
    assert len(extract_text(payload)) <= 1_000_000


def test_multiple_authentication_results_are_kept_in_order() -> None:
    raw = make_message(
        auth=["mx.google.com; dmarc=pass header.from=dept.org", "evil.example; dmarc=pass"]
    )
    assert [h.split(";")[0] for h in parse_message(raw).auth_results] == [
        "mx.google.com",
        "evil.example",
    ]


def test_references_and_missing_optional_headers() -> None:
    raw = make_message(references="<a@x> <b@x>")
    assert parse_message(raw).references == "<a@x> <b@x>"
    assert parse_message(make_message()).references is None


@pytest.mark.parametrize(
    "broken",
    [
        {},
        {"id": "1", "threadId": "t"},
        {"id": "1", "threadId": "t", "payload": {"headers": [{"name": "Subject", "value": "x"}]}},
    ],
)
def test_malformed_messages_are_rejected(broken: dict[str, Any]) -> None:
    with pytest.raises(MessageFormatError):
        parse_message(broken)


def test_unparseable_date_falls_back_to_now() -> None:
    raw = make_message()
    raw["internalDate"] = "not-a-number"
    assert parse_message(raw).received_at.startswith("20")


# ------------------------------------------------------------------ headers
@pytest.mark.parametrize(
    ("header", "address", "name"),
    [
        ("Anita R. <anita@dept.org>", "anita@dept.org", "Anita R."),
        ("ANITA@Dept.ORG", "anita@dept.org", ""),
        ('"Doe, Jane" <jane.doe+x@dept.org>', "jane.doe+x@dept.org", "Doe, Jane"),
        ("=?UTF-8?B?QW5pdGEgUsOpbWk=?= <anita@dept.org>", "anita@dept.org", "Anita Rémi"),
    ],
)
def test_parse_sender(header: str, address: str, name: str) -> None:
    assert parse_sender(header) == (address, name)


@pytest.mark.parametrize(
    "header",
    [
        "",
        "no address here",
        "a@dept.org, b@dept.org",  # two people
        "a@dept.org; b@evil.example",
        "<a@dept.org> <b@dept.org>",
        "x..y@dept.org",
        "a b@dept.org",
        "undisclosed-recipients:;",
    ],
)
def test_parse_sender_rejects_anything_but_one_plain_address(header: str) -> None:
    with pytest.raises(MessageFormatError):
        parse_sender(header)


def test_display_name_is_made_safe() -> None:
    assert sanitize_display_name("Bob\r\nBcc: evil@x.org") == "Bob Bcc: evil@x.org"
    assert "\n" not in sanitize_display_name("a\nb")
    assert len(sanitize_display_name("x" * 500)) == MAX_NAME_CHARS
    assert sanitize_display_name("  <Anita>  ") == "Anita"
    assert decode_header_text("=?bad-charset?Q?x?=") != ""  # never raises


# ------------------------------------------------------------------ sender authentication
GOOD = auth_header("dept.org")


def verdict(headers: list[str], domain: str = "dept.org", **kw: Any) -> Verdict:
    return verify_sender(headers, domain, **kw).verdict


def test_dmarc_pass_is_verified() -> None:
    result = verify_sender([GOOD], "dept.org")
    assert result.verdict == Verdict.VERIFIED
    assert "DMARC" in result.reason


def test_spf_and_dkim_both_passing_is_enough_without_dmarc() -> None:
    header = "mx.google.com; dkim=pass header.d=dept.org; spf=pass smtp.mailfrom=a@dept.org"
    assert verdict([header]) == Verdict.VERIFIED


def test_spf_alone_or_dkim_alone_is_not_enough() -> None:
    assert verdict(["mx.google.com; spf=pass smtp.mailfrom=a@dept.org"]) == Verdict.SUSPICIOUS
    assert verdict(["mx.google.com; dkim=pass header.d=dept.org"]) == Verdict.SUSPICIOUS


@pytest.mark.parametrize(
    "result", ["fail", "softfail", "none", "neutral", "permerror", "temperror"]
)
def test_failed_checks_are_suspicious(result: str) -> None:
    assert (
        verdict([auth_header("dept.org", dmarc=result, spf=result, dkim=result)])
        == Verdict.SUSPICIOUS
    )


def test_a_pass_for_another_domain_proves_nothing() -> None:
    assert verdict([auth_header("evil.example")], domain="dept.org") == Verdict.SUSPICIOUS
    lookalike = auth_header("dept.org.evil.example")
    assert verdict([lookalike], domain="dept.org") == Verdict.SUSPICIOUS
    assert verdict([auth_header("notdept.org")], domain="dept.org") == Verdict.SUSPICIOUS


def test_headers_from_untrusted_servers_are_ignored() -> None:
    forged = auth_header("dept.org", server="evil.example")
    result = verify_sender([forged], "dept.org")
    assert result.verdict == Verdict.UNKNOWN
    assert "trusted" in result.reason


def test_only_the_topmost_trusted_header_counts() -> None:
    """A forged header a sender wrote sits BELOW the one the receiving server added."""
    ours_fail = auth_header("dept.org", dmarc="fail", spf="fail", dkim="fail")
    forged_pass = auth_header("dept.org")  # claims to be mx.google.com but is lower down
    assert verdict([ours_fail, forged_pass]) == Verdict.SUSPICIOUS
    assert verdict([auth_header("evil.example", server="other"), GOOD]) == Verdict.VERIFIED


def test_missing_header_is_unknown_not_verified() -> None:
    assert verdict([]) == Verdict.UNKNOWN


def test_turning_authentication_off_is_explicit() -> None:
    result = verify_sender([], "dept.org", require=False)
    assert result.verdict == Verdict.VERIFIED
    assert "turned off" in result.reason


def test_custom_trusted_server_and_case_insensitivity() -> None:
    header = auth_header("dept.org", server="MX.Mail.Example.COM")
    assert verdict([header], trusted_servers=["mx.mail.example.com"]) == Verdict.VERIFIED
    assert verdict([header]) == Verdict.UNKNOWN


def test_comments_and_odd_spacing_do_not_confuse_the_parser() -> None:
    header = "mx.google.com;\r\n dmarc = pass (p=NONE; sp=NONE) header.from=dept.org ;"
    assert verdict([header]) == Verdict.VERIFIED
    assert verdict(["garbage ;;; ==="]) == Verdict.UNKNOWN


def test_subdomain_alignment() -> None:
    assert aligned("mail.dept.org", "dept.org")
    assert aligned("dept.org", "mail.dept.org")
    assert aligned("DEPT.org.", "dept.ORG")
    assert not aligned("org", "dept.org")  # a bare suffix aligns with nothing
    assert not aligned("dept.org", "")
    assert not aligned("evil-dept.org", "dept.org")

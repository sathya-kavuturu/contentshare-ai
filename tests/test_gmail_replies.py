from __future__ import annotations

from pathlib import Path

import pytest

from contentshare.gmail.replies import ReplyError, ReplyResult
from contentshare.gmail.reply import (
    MAX_BODY_CHARS,
    build_reply,
    clean_body_text,
    reply_subject,
    threading_headers,
)
from contentshare.jobs.states import RequestState
from contentshare.storage.db import Database
from fake_gmail import auth_header, http_error, make_message
from helpers import MailWorld, build_mail_world

S = RequestState
EXACT = "Sunrise Talk 12 Oct 2026 English recording"


@pytest.fixture
def w(tmp_path: Path, db: Database) -> MailWorld:
    return build_mail_world(tmp_path, db)


def verified_request(w: MailWorld, **message: object) -> int:
    """An email that went through intake, was approved, and whose copy was verified."""
    fields: dict[str, object] = {"message_id": "m1", "subject": EXACT, "body": "Please share."}
    fields.update(message)
    w.gmail.add(make_message(**fields))  # type: ignore[arg-type]
    (request_id,) = w.intake.check_now().request_ids
    w.repo.transition(request_id, S.APPROVED, actor="user")
    w.repo.transition(request_id, S.COPYING, actor="system")
    w.repo.transition(
        request_id, S.VERIFIED, actor="system", file_count=3, total_bytes=2510 * 1024 * 1024
    )
    return request_id


def request_state(w: MailWorld, request_id: int) -> str:
    row = w.repo.get_request(request_id)
    assert row is not None
    return str(row["state"])


# ------------------------------------------------------------------ the message itself
def test_subject_prefix_is_not_doubled() -> None:
    assert reply_subject("Sunrise Talk") == "Re: Sunrise Talk"
    assert reply_subject("Re: RE: Sunrise Talk") == "Re: Sunrise Talk"
    assert reply_subject("") == "Re: your request"


def test_threading_headers() -> None:
    assert threading_headers("<a@x.org>", None) == {
        "In-Reply-To": "<a@x.org>",
        "References": "<a@x.org>",
    }
    chained = threading_headers("<c@x.org>", "<a@x.org> <b@x.org>")
    assert chained["References"] == "<a@x.org> <b@x.org> <c@x.org>"
    assert threading_headers(None, "<a@x.org>") == {}
    assert threading_headers("not an id", "<a@x.org>") == {}


def test_malformed_references_are_dropped_and_long_chains_trimmed() -> None:
    headers = threading_headers(
        "<p@x.org>",
        "junk <ok@x.org> <evil@x.org>\r\nBcc: z@x.org "
        + " ".join(f"<r{i}@x.org>" for i in range(60)),
    )
    refs = headers["References"].split()
    assert refs[-1] == "<p@x.org>"
    assert len(refs) <= 20
    assert "Bcc" not in headers["References"]
    assert all(r.startswith("<") and r.endswith(">") for r in refs)


def test_body_is_cleaned_and_limited() -> None:
    assert clean_body_text("a\r\nb\rc\x00d\x07") == "a\nb\ncd\n"
    assert len(clean_body_text("x" * 100_000)) <= MAX_BODY_CHARS + 1


@pytest.mark.parametrize(
    "bad",
    ["a@dept.org\r\nBcc: evil@x.org", "a@dept.org\nCc: evil@x.org"],
)
def test_header_injection_is_refused_by_the_message_builder(bad: str) -> None:
    with pytest.raises(ValueError):
        build_reply(
            to_addr=bad,
            subject="s",
            body="b",
            token="t",
            original_message_id=None,
            original_references=None,
        )
    with pytest.raises(ValueError):
        build_reply(
            to_addr="a@dept.org",
            subject="s" + bad,
            body="b",
            token="t",
            original_message_id=None,
            original_references=None,
        )


# ------------------------------------------------------------------ drafting
def test_default_template_is_filled_in(w: MailWorld) -> None:
    request_id = verified_request(w)
    draft = w.replies.draft_shared(request_id)
    assert draft.to_addr == "anita@dept.org"
    assert draft.subject == f"Re: {EXACT}"
    assert draft.thread_id.startswith("t")
    assert "Hello Anita R.," in draft.body
    assert "  - 2026-10-12_SunriseTalk_EN" in draft.body.splitlines()  # on its own line
    assert "Total: 3 files, 2.5 GB." in draft.body
    assert "{" not in draft.body
    # the network location of the destination is private: no path of any kind
    assert str(w.media) not in draft.body and chr(92) not in draft.body and ":/" not in draft.body


def test_department_template_wins_and_missing_name_falls_back(w: MailWorld) -> None:
    request_id = verified_request(w, from_="cfo@finance.org", subject="Annual Day 2025")
    draft = w.replies.draft_shared(request_id)
    assert draft.body.startswith("Dear there, Annual Day 2025 (3 files) is at ")


def test_date_defaults_to_today_when_the_request_has_none(w: MailWorld) -> None:
    request_id = verified_request(w, subject="Annual Day 2025", message_id="m2")
    w.replies.config = w.replies.config.model_copy(
        update={"default_reply_template": "Shared on {date}."}
    )
    assert "2026-11-03" in w.replies.draft_shared(request_id).body


def test_hostile_sender_name_is_just_text_in_the_greeting(w: MailWorld) -> None:
    request_id = verified_request(
        w, from_='"Bob Bcc: evil@x.org" <anita@dept.org>', auth=auth_header("dept.org")
    )
    draft = w.replies.draft_shared(request_id)
    assert draft.body.startswith("Hello Bob Bcc: evil@x.org,")
    w.replies.send(draft)
    message = w.gmail.sent[0]["message"]
    assert message["To"] == "anita@dept.org"
    assert message.get_all("Bcc") is None


def test_sender_header_with_a_line_break_is_rejected_outright(w: MailWorld) -> None:
    hostile_from = "Bob" + chr(13) + chr(10) + "Bcc: evil@x.org <anita@dept.org>"
    w.gmail.add(make_message(message_id="m1", from_=hostile_from, subject=EXACT))
    report = w.intake.check_now()
    assert (report.requests, report.ignored) == (0, 1)


# ------------------------------------------------------------------ sending
def test_reply_goes_to_the_sender_in_the_same_thread(w: MailWorld) -> None:
    request_id = verified_request(
        w, rfc_id="<orig@mail.dept.org>", references="<earlier@mail.dept.org>"
    )
    result = w.replies.send(w.replies.draft_shared(request_id))

    assert result.status == "SENT"
    assert request_state(w, request_id) == "REPLIED"
    (sent,) = w.gmail.sent
    original_thread = w.gmail.messages["m1"]["threadId"]
    assert sent["threadId"] == original_thread
    message = sent["message"]
    assert message["To"] == "anita@dept.org"
    assert message["Subject"] == f"Re: {EXACT}"
    assert message["In-Reply-To"] == "<orig@mail.dept.org>"
    assert message["References"] == "<earlier@mail.dept.org> <orig@mail.dept.org>"
    assert "X-ContentShare-Reply" in message
    assert message.get_all("Bcc") is None
    assert "Hello Anita R." in message.get_content()

    reply = w.repo.get_reply(result.reply_id or 0)
    assert reply is not None
    assert (reply["status"], reply["purpose"], reply["gmail_message_id"]) == (
        "SENT",
        "shared",
        "sent1",
    )
    assert reply["sent_at"]
    assert [e["event"] for e in w.repo.audit_entries(request_id)][-3:] == [
        "state_changed", "reply_sent", "reply_confirmed",
    ]  # fmt: skip


def test_reply_to_header_is_never_used(w: MailWorld) -> None:
    request_id = verified_request(w, extra_headers=[("Reply-To", "attacker@evil.example")])
    w.replies.send(w.replies.draft_shared(request_id))
    assert w.gmail.sent[0]["message"]["To"] == "anita@dept.org"
    assert "evil" not in w.gmail.sent[0]["raw"].decode("utf-8", errors="replace")


def test_edited_text_is_what_gets_sent(w: MailWorld) -> None:
    request_id = verified_request(w)
    draft = w.replies.draft_shared(request_id).with_body("Shared. नमस्ते \U0001f64f")
    w.replies.send(draft)
    assert w.gmail.sent[0]["message"].get_content().strip() == "Shared. नमस्ते \U0001f64f"


def test_unusable_original_message_id_still_threads_by_gmail_thread(w: MailWorld) -> None:
    request_id = verified_request(w, rfc_id="garbage no brackets")
    w.replies.send(w.replies.draft_shared(request_id))
    message = w.gmail.sent[0]["message"]
    assert message["In-Reply-To"] is None
    assert w.gmail.sent[0]["threadId"] == w.gmail.messages["m1"]["threadId"]


def test_hostile_subject_cannot_add_headers(w: MailWorld) -> None:
    request_id = verified_request(w, subject="Sunrise Talk 12 Oct 2026 English\r\nBcc: evil@x.org")
    w.replies.send(w.replies.draft_shared(request_id))
    message = w.gmail.sent[0]["message"]
    assert message.get_all("Bcc") is None
    assert "\n" not in str(message["Subject"]).strip()


# ------------------------------------------------------------------ the rules
@pytest.mark.parametrize("state", [S.READY, S.NEEDS_REVIEW, S.APPROVED, S.COPYING, S.FAILED])
def test_no_shared_reply_unless_the_copy_is_verified(w: MailWorld, state: RequestState) -> None:
    w.gmail.add(make_message(message_id="m1", subject=EXACT))
    (request_id,) = w.intake.check_now().request_ids
    with w.repo.db.transaction() as conn:  # put the request in the state under test
        conn.execute("UPDATE requests SET state = ? WHERE id = ?", (str(state), request_id))
    with pytest.raises(ReplyError, match="not been verified"):
        w.replies.draft_shared(request_id)
    forged = w.replies._draft(w.repo.get_request(request_id), "shared", "All done!")  # type: ignore[arg-type]
    with pytest.raises(ReplyError, match="not been verified"):
        w.replies.send(forged)
    assert w.gmail.sent == []
    assert w.repo.list_replies() == []


def test_a_failed_copy_can_never_be_announced(w: MailWorld) -> None:
    request_id = verified_request(w)
    # simulate: the same request is later found to have failed
    with w.repo.db.transaction() as conn:
        conn.execute("UPDATE requests SET state = 'FAILED' WHERE id = ?", (request_id,))
    draft = w.replies._draft(w.repo.get_request(request_id), "shared", "Content shared!")  # type: ignore[arg-type]
    with pytest.raises(ReplyError):
        w.replies.send(draft)
    assert w.gmail.sent == []


def test_reply_cannot_be_redirected_to_someone_else(w: MailWorld) -> None:
    request_id = verified_request(w)
    draft = w.replies.draft_shared(request_id)
    from dataclasses import replace

    with pytest.raises(ReplyError, match="person who made the request"):
        w.replies.send(replace(draft, to_addr="attacker@evil.example"))
    assert w.gmail.sent == []


@pytest.mark.parametrize("body", ["", "   \n  ", "x" * (MAX_BODY_CHARS + 1)])
def test_empty_or_huge_replies_are_refused(w: MailWorld, body: str) -> None:
    request_id = verified_request(w)
    with pytest.raises(ReplyError):
        w.replies.send(w.replies.draft_shared(request_id).with_body(body))
    assert w.repo.list_replies() == []


def test_unknown_kind_and_unknown_request(w: MailWorld) -> None:
    request_id = verified_request(w)
    from dataclasses import replace

    with pytest.raises(ReplyError, match="Unknown kind"):
        w.replies.send(replace(w.replies.draft_shared(request_id), purpose="newsletter"))
    with pytest.raises(ReplyError, match="No request"):
        w.replies.draft_shared(999)


def test_second_send_is_blocked(w: MailWorld) -> None:
    request_id = verified_request(w)
    draft = w.replies.draft_shared(request_id)
    w.replies.send(draft)
    with pytest.raises(ReplyError):  # already REPLIED
        w.replies.send(draft)
    assert len(w.gmail.sent) == 1


def test_the_database_itself_prevents_two_active_replies(w: MailWorld) -> None:
    """A double click or a second process: the unique rule, not just our checks, stops it."""
    request_id = verified_request(w)
    w.repo.begin_reply(
        request_id, purpose="shared", to_addr="anita@dept.org", body="x", token="one"
    )
    with pytest.raises(ReplyError, match="already been sent or is being sent"):
        w.replies.send(w.replies.draft_shared(request_id))
    assert w.gmail.sent == []


# ------------------------------------------------------------------ failures
def test_refused_send_leaves_the_request_verified_and_retryable(w: MailWorld) -> None:
    request_id = verified_request(w)
    w.gmail.fail("messages.send", http_error(403, "insufficientPermissions"))
    result = w.replies.send(w.replies.draft_shared(request_id))
    assert result.status == "FAILED"
    assert "refused" in (result.error or "")
    assert request_state(w, request_id) == "VERIFIED"
    assert w.gmail.sent == []

    retry = w.replies.send(w.replies.draft_shared(request_id))  # the Retry button
    assert retry.status == "SENT"
    assert request_state(w, request_id) == "REPLIED"
    assert [r["status"] for r in w.repo.list_replies(request_id)] == ["FAILED", "SENT"]


def test_dropped_connection_before_anything_was_sent_is_a_clean_failure(w: MailWorld) -> None:
    request_id = verified_request(w)
    w.gmail.fail("messages.send", http_error(503))
    result = w.replies.send(w.replies.draft_shared(request_id))
    assert result.status == "FAILED"
    assert "Not sent" in (result.error or "")
    assert request_state(w, request_id) == "VERIFIED"
    assert w.replies.send(w.replies.draft_shared(request_id)).status == "SENT"
    assert len(w.gmail.sent) == 1


def test_dropped_connection_after_gmail_accepted_it_does_not_send_twice(w: MailWorld) -> None:
    request_id = verified_request(w)
    w.gmail.drop_response_on_send = True  # stored by Gmail, but we only see a timeout
    result = w.replies.send(w.replies.draft_shared(request_id))
    assert result.status == "SENT"  # found in the thread by its tag
    assert result.gmail_message_id == "sent1"
    assert request_state(w, request_id) == "REPLIED"
    assert len(w.gmail.sent) == 1
    with pytest.raises(ReplyError):
        w.replies.send(w.replies.draft_shared(request_id))
    assert len(w.gmail.sent) == 1


def test_if_we_cannot_even_check_the_thread_the_reply_stays_pending(w: MailWorld) -> None:
    request_id = verified_request(w)
    w.gmail.drop_response_on_send = True
    w.gmail.hide_thread = True
    result = w.replies.send(w.replies.draft_shared(request_id))
    assert result.status == "UNCERTAIN"
    assert request_state(w, request_id) == "VERIFIED"  # not claimed as done
    (pending,) = w.repo.list_replies(request_id)
    assert pending["status"] == "SENDING"
    with pytest.raises(ReplyError):  # and no second copy can be started meanwhile
        w.replies.send(w.replies.draft_shared(request_id))

    assert w.replies.recover_unfinished()[0].status == "UNCERTAIN"  # still offline
    w.gmail.hide_thread = False
    (recovered,) = w.replies.recover_unfinished()
    assert recovered.status == "SENT"
    assert request_state(w, request_id) == "REPLIED"
    assert len(w.gmail.sent) == 1


def test_a_crash_before_sending_is_recovered_as_not_sent(w: MailWorld) -> None:
    request_id = verified_request(w)
    w.repo.begin_reply(
        request_id, purpose="shared", to_addr="anita@dept.org", body="x", token="lost"
    )
    (result,) = w.replies.recover_unfinished()  # the app died before calling Gmail
    assert result.status == "FAILED"
    assert request_state(w, request_id) == "VERIFIED"
    assert w.replies.send(w.replies.draft_shared(request_id)).status == "SENT"


def test_a_crash_after_sending_is_recovered_as_sent(w: MailWorld) -> None:
    request_id = verified_request(w)
    draft = w.replies.draft_shared(request_id)
    token = "crash-token"
    reply_id = w.repo.begin_reply(
        request_id, purpose="shared", to_addr=draft.to_addr, body=draft.body, token=token
    )
    message = build_reply(
        to_addr=draft.to_addr,
        subject=draft.subject,
        body=draft.body,
        token=token,
        original_message_id=None,
        original_references=None,
    )
    from contentshare.gmail.reply import encode_raw

    w.gateway.send_raw(encode_raw(message), draft.thread_id)  # Gmail accepted it; then we "crashed"
    (result,) = w.replies.recover_unfinished()
    assert (result.status, result.reply_id) == ("SENT", reply_id)
    assert request_state(w, request_id) == "REPLIED"
    assert len(w.gmail.sent) == 1


def test_recovery_with_nothing_pending_is_a_no_op(w: MailWorld) -> None:
    assert w.replies.recover_unfinished() == []


# ------------------------------------------------------------------ rejection replies
def test_rejection_reply_for_a_rejected_request(w: MailWorld) -> None:
    w.gmail.add(make_message(message_id="m1", subject="Quantum Lecture"))
    (request_id,) = w.intake.check_now().request_ids
    w.repo.transition(request_id, S.REJECTED, actor="user")
    draft = w.replies.draft_rejection(request_id)
    assert "unable to share" in draft.body
    assert w.replies.send(draft).status == "SENT"
    assert request_state(w, request_id) == "REJECTED"  # a rejection reply changes no state

    request_two = w.repo.add_request(1, 2, {})
    w.repo.transition(request_two, S.REJECTED, actor="user")
    custom = w.replies.draft_rejection(request_two, "Sorry, that content is not available.")
    assert custom.body == "Sorry, that content is not available."


def test_rejection_replies_only_for_rejected_requests(w: MailWorld) -> None:
    request_id = verified_request(w)
    with pytest.raises(ReplyError, match="rejected"):
        w.replies.draft_rejection(request_id)


def test_shared_reply_not_allowed_for_a_rejected_request(w: MailWorld) -> None:
    w.gmail.add(make_message(message_id="m1", subject="Quantum Lecture"))
    (request_id,) = w.intake.check_now().request_ids
    w.repo.transition(request_id, S.REJECTED, actor="user")
    with pytest.raises(ReplyError):
        w.replies.draft_shared(request_id)


# ------------------------------------------------------------------ automatic reply
def test_auto_reply_sends_when_asked_for_and_verified(w: MailWorld) -> None:
    w.rebuild()
    w.config = w.config.model_copy(update={"auto_reply_default": True})
    w.intake.config = w.config
    request_id = verified_request(w)
    result = w.replies.maybe_auto_reply(request_id)
    assert isinstance(result, ReplyResult)
    assert result.status == "SENT"
    assert request_state(w, request_id) == "REPLIED"
    assert w.replies.maybe_auto_reply(request_id) is None  # never twice


def test_auto_reply_does_nothing_when_not_requested_or_not_verified(w: MailWorld) -> None:
    request_id = verified_request(w)  # auto_reply defaults to off
    assert w.replies.maybe_auto_reply(request_id) is None
    assert w.gmail.sent == []

    w.gmail.add(make_message(message_id="m2", subject="Annual Day 2025", thread_id="t2"))
    other = next(i for i in w.intake.check_now().request_ids)
    with w.repo.db.transaction() as conn:
        conn.execute("UPDATE requests SET auto_reply = 1 WHERE id = ?", (other,))
    assert w.replies.maybe_auto_reply(other) is None  # still READY, never copied
    assert w.gmail.sent == []

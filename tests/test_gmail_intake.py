from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from contentshare.config.models import AppConfig, SmartSettings
from contentshare.gmail.errors import AuthRequiredError
from contentshare.gmail.intake import build_queries, label_search_name
from contentshare.storage.db import Database
from fake_gmail import auth_header, http_error, make_message
from helpers import MailWorld, build_mail_world, make_storage

EXACT = "Sunrise Talk 12 Oct 2026 English recording"


@pytest.fixture
def w(tmp_path: Path, db: Database) -> MailWorld:
    return build_mail_world(tmp_path, db)


def state_of(w: MailWorld, request_id: int) -> str:
    row = w.repo.get_request(request_id)
    assert row is not None
    return str(row["state"])


# ------------------------------------------------------------------ queries
def test_queries_only_ask_for_configured_senders(w: MailWorld) -> None:
    (query,) = build_queries(w.config)
    assert query.startswith("from:(")
    for sender in ("anita@dept.org", "editor@dept.org", "finance.org"):
        assert sender in query
    assert "newer_than:30d" in query
    assert "-label:contentshareai-processed" in query


def test_queries_are_split_for_many_senders() -> None:
    depts = [
        {"name": f"D{i}", "sender_emails": [f"p{i}@d{i}.org"], "destination_root": f"C:\\d{i}"}
        for i in range(40)
    ]
    queries = build_queries(AppConfig(departments=depts))
    assert len(queries) == 3
    assert all(q.count("@") <= 15 for q in queries)


def test_no_label_clause_when_labels_are_off(w: MailWorld) -> None:
    w.rebuild(apply_label=False)
    assert "-label" not in build_queries(w.config)[0]
    assert label_search_name("My Team/Done") == "my-team-done"


# ------------------------------------------------------------------ the main flow
def test_new_requests_become_ready_and_review_lines(w: MailWorld) -> None:
    w.gmail.add(make_message(message_id="a", subject=EXACT, body="Hi team, please share."))
    w.gmail.add(make_message(message_id="b", subject="Quantum Lecture", body="Need it"))
    w.gmail.add(
        make_message(message_id="c", from_="CFO <cfo@finance.org>", subject="Annual Day 2025")
    )

    report = w.intake.check_now()

    assert (report.examined, report.requests, report.ready, report.needs_review) == (3, 3, 2, 1)
    assert not report.errors
    assert not report.needs_sign_in
    states = {r["gmail_message_id"]: r["state"] for r in w.repo.list_requests()}
    assert states == {"a": "READY", "b": "NEEDS_REVIEW", "c": "READY"}
    finance = next(r for r in w.repo.list_requests() if r["gmail_message_id"] == "c")
    assert finance["department"] == "Finance"
    assert finance["destination_path"].endswith("AnnualDay_2025-12-25")


def test_email_headers_for_threading_are_kept(w: MailWorld) -> None:
    w.gmail.add(
        make_message(
            message_id="a", subject=EXACT, rfc_id="<abc@mail.dept.org>", references="<r1@x.org>"
        )
    )
    w.intake.check_now()
    (row,) = w.repo.list_requests()
    email = w.repo.get_email(1)
    assert email is not None
    assert email["rfc_message_id"] == "<abc@mail.dept.org>"
    assert email["references_header"] == "<r1@x.org>"
    assert row["gmail_thread_id"] == email["gmail_thread_id"]


def test_each_message_is_processed_once(w: MailWorld) -> None:
    w.gmail.add(make_message(message_id="a", subject=EXACT))
    w.intake.check_now()
    fetched = w.gmail.calls.count("messages.get")
    again = w.intake.check_now()
    assert again.examined == 0
    assert w.gmail.calls.count("messages.get") == fetched  # not even downloaded again
    assert len(w.repo.list_requests()) == 1


def test_processed_label_is_added_and_excluded_next_time(w: MailWorld) -> None:
    w.gmail.add(make_message(message_id="a", subject=EXACT))
    w.intake.check_now()
    assert "Label_1" in w.gmail.label_ids_of("a")
    assert w.gmail.labels["Label_1"] == "ContentShareAI/Processed"
    w.intake.check_now()
    # the second search no longer even lists the labelled message
    department = [q for q in w.gmail.queries if q.startswith("from:(")][-1]
    assert w.gateway.list_message_ids(department, 10) == []


def test_labels_off_means_no_label_calls(w: MailWorld) -> None:
    w.rebuild(apply_label=False)
    w.gmail.add(make_message(message_id="a", subject=EXACT))
    w.intake.check_now()
    assert not any(c.startswith("labels.") or c == "messages.modify" for c in w.gmail.calls)
    assert w.intake.check_now().examined == 0  # the database alone prevents reprocessing


def test_label_permission_problem_is_a_warning_not_a_failure(w: MailWorld) -> None:
    w.gmail.add(make_message(message_id="a", subject=EXACT))
    w.gmail.fail("labels.list", http_error(403, "insufficientPermissions"))
    report = w.intake.check_now()
    assert report.requests == 1
    assert any("labels" in x for x in report.warnings)
    assert not report.needs_sign_in
    assert state_of(w, report.request_ids[0]) == "READY"


# ------------------------------------------------------------------ who is allowed
def test_senders_outside_the_list_are_ignored_and_remembered(w: MailWorld) -> None:
    """A Gmail search can match more loosely than we want, so every message is re-checked."""
    w.gmail.loose_from_match = True
    w.intake.config = w.config.model_copy(update={"smart": SmartSettings(read_unlisted=False)})
    w.gmail.add(make_message(message_id="evil", from_="Evil <x@notfinance.org>", subject=EXACT))
    w.gmail.add(make_message(message_id="good", subject=EXACT))
    report = w.intake.check_now()
    assert (report.ignored, report.requests) == (1, 1)
    assert w.repo.is_processed("evil")
    assert "evil" not in {r["gmail_message_id"] for r in w.repo.list_requests()}
    assert "Label_1" not in w.gmail.label_ids_of("evil")  # their mail is left untouched
    again = w.intake.check_now()
    assert again.examined == 0  # and never downloaded again
    ignored = w.repo.get_email(1)
    assert ignored is not None
    assert "not in any department" in ignored["ignored_reason"]


def test_our_own_sent_mail_is_ignored(w: MailWorld) -> None:
    w.gmail.add(make_message(message_id="s", subject=EXACT, labels=("SENT",)))
    report = w.intake.check_now()
    assert (report.ignored, report.requests) == (1, 0)


def test_thanks_in_a_handled_thread_is_not_a_new_request(w: MailWorld) -> None:
    w.gmail.add(make_message(message_id="first", thread_id="T1", subject=EXACT))
    w.intake.check_now()
    w.gmail.add(
        make_message(
            message_id="thanks", thread_id="T1", subject="Re: " + EXACT, body="Thanks, received!"
        )
    )
    report = w.intake.check_now()
    assert (report.ignored, report.requests) == (1, 0)


def test_a_new_request_in_an_old_thread_is_still_handled(w: MailWorld) -> None:
    w.gmail.add(make_message(message_id="first", thread_id="T1", subject=EXACT))
    w.intake.check_now()
    w.gmail.add(
        make_message(
            message_id="more",
            thread_id="T1",
            subject="Re: " + EXACT,
            body="Also need the Annual Day 2025 recording",
        )
    )
    report = w.intake.check_now()
    assert report.requests == 1


# ------------------------------------------------------------------ sender verification
@pytest.mark.parametrize(
    ("auth", "expected_auth", "expected_state"),
    [
        ("default", "verified", "READY"),
        (
            auth_header("dept.org", dmarc="fail", spf="fail", dkim="fail"),
            "suspicious",
            "NEEDS_REVIEW",
        ),
        (auth_header("evil.example"), "suspicious", "NEEDS_REVIEW"),
        (auth_header("dept.org", server="evil.example"), "unknown", "NEEDS_REVIEW"),
        (None, "unknown", "NEEDS_REVIEW"),
    ],
)
def test_sender_verification_decides_ready_or_review(
    w: MailWorld, auth: str | None, expected_auth: str, expected_state: str
) -> None:
    w.gmail.add(make_message(message_id="a", subject=EXACT, auth=auth))
    report = w.intake.check_now()
    email = w.repo.get_email(1)
    assert email is not None
    assert email["auth_status"] == expected_auth
    assert state_of(w, report.request_ids[0]) == expected_state


def test_spoofed_sender_is_flagged_with_a_reason_in_the_audit_log(w: MailWorld) -> None:
    w.gmail.add(
        make_message(
            message_id="a", subject=EXACT, auth=auth_header("dept.org", dmarc="fail", spf="fail")
        )
    )
    w.intake.check_now()
    entry = next(e for e in w.repo.audit_entries() if e["event"] == "sender_checked")
    detail = json.loads(entry["detail"])
    assert detail["verdict"] == "suspicious"
    assert "dmarc=fail" in detail["reason"]


def test_authentication_can_be_turned_off_in_settings(w: MailWorld) -> None:
    w.rebuild(require_authentication=False)
    w.gmail.add(make_message(message_id="a", subject=EXACT, auth=None))
    report = w.intake.check_now()
    assert state_of(w, report.request_ids[0]) == "READY"


# ------------------------------------------------------------------ failures
def test_sign_in_needed_stops_everything_cleanly(w: MailWorld) -> None:
    w.gmail.add(make_message(message_id="a", subject=EXACT))
    w.gmail.fail("messages.list", http_error(401, "authError"))
    report = w.intake.check_now()
    assert report.needs_sign_in
    assert report.examined == 0
    assert w.repo.list_requests() == []


def test_sign_in_lost_midway_keeps_what_was_done(w: MailWorld) -> None:
    w.gmail.add(make_message(message_id="a", subject=EXACT, internal_ms=1_793_000_002_000))
    w.gmail.add(
        make_message(message_id="b", subject="Annual Day 2025", internal_ms=1_793_000_001_000)
    )
    original = w.gateway.get_message
    calls = {"n": 0}

    def revoked_on_second(message_id: str):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 2:
            raise AuthRequiredError("access revoked")
        return original(message_id)

    w.gateway.get_message = revoked_on_second  # type: ignore[method-assign]
    report = w.intake.check_now()
    assert report.needs_sign_in
    assert len(w.repo.list_requests()) == 1  # the first message was fully handled
    assert not w.repo.is_processed("b")  # the second will be retried after reconnecting


def test_unreadable_message_is_recorded_and_does_not_stop_the_rest(w: MailWorld) -> None:
    """A From header with two people cannot be used. The search matches it by its first address,
    so it reaches us; it must be set aside without blocking the good message after it."""
    w.gmail.add(make_message(message_id="odd", subject="x", internal_ms=1_793_000_003_000))
    for header in w.gmail.messages["odd"]["payload"]["headers"]:
        if header["name"] == "From":
            header["value"] = "anita@dept.org, bob@dept.org"
    w.gmail.add(make_message(message_id="good", subject=EXACT, internal_ms=1_793_000_002_000))

    report = w.intake.check_now()

    assert (report.ignored, report.requests) == (1, 1)
    odd = next(e for e in w.repo.audit_entries() if e["event"] == "email_ignored")
    assert "unreadable" in json.loads(odd["detail"])["reason"]
    assert w.repo.is_processed("odd")
    assert w.intake.check_now().examined == 0  # never retried forever


def test_temporary_failure_on_one_message_is_retried_next_time(w: MailWorld) -> None:
    w.gmail.add(make_message(message_id="a", subject=EXACT, internal_ms=1_793_000_002_000))
    w.gmail.add(
        make_message(message_id="b", subject="Annual Day 2025", internal_ms=1_793_000_001_000)
    )
    w.gmail.fail(
        "messages.get", *[http_error(500) for _ in range(4)]
    )  # first message keeps failing
    first = w.intake.check_now()
    assert len(first.errors) == 1
    assert first.requests == 1  # the other message went through
    assert not w.repo.is_processed("a")
    second = w.intake.check_now()
    assert second.requests == 1
    assert w.repo.is_processed("a")


def test_listing_failure_is_reported(w: MailWorld) -> None:
    w.gmail.fail("messages.list", *[http_error(500) for _ in range(4)])
    report = w.intake.check_now()
    assert report.errors
    assert report.examined == 0


# ------------------------------------------------------------------ limits & control
def test_paging_and_the_per_check_limit(tmp_path: Path, db: Database) -> None:
    w = build_mail_world(tmp_path, db, page_size=3)
    w.rebuild(max_messages=7)
    for i in range(12):
        w.gmail.add(
            make_message(
                message_id=f"m{i}", subject=f"Quantum {i}", internal_ms=1_793_000_000_000 + i
            )
        )
    assert w.intake.check_now().examined == 7
    assert w.intake.check_now().examined == 5  # the rest on the next check


def test_stop_request_is_honoured(w: MailWorld) -> None:
    for i in range(5):
        w.gmail.add(
            make_message(
                message_id=f"m{i}", subject=f"Quantum {i}", internal_ms=1_793_000_000_000 + i
            )
        )
    seen = {"n": 0}

    def stop_after_two() -> bool:
        seen["n"] += 1
        return seen["n"] > 2

    report = w.intake.check_now(should_stop=stop_after_two)
    assert report.stopped
    assert report.examined == 2


def test_index_can_be_refreshed_before_checking(w: MailWorld) -> None:
    make_storage(w.storage, {"2026/2026-11-01_BrandNewTalk_EN/video.mxf": 10})
    w.gmail.add(make_message(message_id="a", subject="Brand New Talk 1 Nov 2026 English"))
    without = w.intake.check_now()
    assert state_of(w, without.request_ids[0]) == "NEEDS_REVIEW"  # not in the index yet

    w.gmail.add(
        make_message(message_id="b", subject="Brand New Talk 1 Nov 2026 English", thread_id="other")
    )
    with_refresh = w.intake.check_now(refresh_index=True)
    assert state_of(w, with_refresh.request_ids[0]) == "READY"


def test_unreachable_storage_during_refresh_is_a_warning(w: MailWorld) -> None:
    w.config.source_roots.append(str(w.storage.parent / "offline-server"))
    w.gmail.add(make_message(message_id="a", subject=EXACT))
    report = w.intake.check_now(refresh_index=True)
    assert any("offline-server" in x for x in report.warnings)
    assert report.requests == 1


# ------------------------------------------------------------------ privacy
def test_email_text_never_reaches_the_logs_or_the_audit_trail(
    w: MailWorld, caplog: pytest.LogCaptureFixture
) -> None:
    """Bodies are kept out of both. (The parsed program name does appear in a decision's
    reason in the audit trail, because the review screen shows it; the subject never reaches
    the program logs.)"""
    caplog.set_level(logging.DEBUG)
    w.gmail.add(
        make_message(message_id="a", subject="TOPSECRETSUBJECT", body="PRIVATEBODYTEXT hunter2")
    )
    w.gmail.add(make_message(message_id="b", subject=EXACT, body="PRIVATEBODYTEXT"))
    w.gmail.fail("messages.get", http_error(500, message="PRIVATEBODYTEXT"))
    w.intake.check_now()
    w.intake.check_now()
    logged = " ".join(r.getMessage() for r in caplog.records)
    for secret in ("PRIVATEBODYTEXT", "TOPSECRETSUBJECT", "hunter2"):
        assert secret not in logged
    audit = json.dumps(w.repo.audit_entries())
    assert "PRIVATEBODYTEXT" not in audit
    assert "hunter2" not in audit


def test_a_check_leaves_an_audit_trail(w: MailWorld) -> None:
    w.gmail.add(make_message(message_id="a", subject=EXACT))
    w.intake.check_now()
    events = [e["event"] for e in w.repo.audit_entries()]
    assert {
        "email_recorded",
        "sender_checked",
        "request_created",
        "state_changed",
        "mail_checked",
    } <= set(events)
    summary = json.loads(
        next(e for e in w.repo.audit_entries() if e["event"] == "mail_checked")["detail"]
    )
    assert (summary["examined"], summary["requests"], summary["ready"]) == (1, 1, 1)


# ------------------------------------------------------------------ peek (read-only)
def test_peek_shows_each_sender_verdict_and_changes_nothing(w: MailWorld) -> None:
    from contentshare.gmail.intake import peek

    w.gmail.add(make_message(message_id="ok", subject=EXACT, internal_ms=1_793_000_003_000))
    w.gmail.add(
        make_message(
            message_id="spoof",
            subject="Annual Day",
            internal_ms=1_793_000_002_000,
            auth=auth_header("dept.org", dmarc="fail", spf="fail", dkim="fail"),
        )
    )
    w.gmail.add(
        make_message(message_id="none", subject="x", auth=None, internal_ms=1_793_000_001_000)
    )
    rows = peek(w.gateway, w.config, limit=10)

    assert [(r.message_id, r.verdict) for r in rows] == [
        ("ok", "verified"),
        ("spoof", "suspicious"),
        ("none", "unknown"),
    ]
    assert rows[0].department == "Media Team"
    assert "dmarc=fail" in rows[1].reason
    # read only: no records, no labels, no sends
    assert w.repo.list_requests() == []
    assert not w.repo.is_processed("ok")
    assert "messages.modify" not in w.gmail.calls
    assert "messages.send" not in w.gmail.calls
    assert all(m["labelIds"] == ["INBOX"] for m in w.gmail.messages.values())


def test_peek_respects_the_limit_and_reports_unreadable_mail(w: MailWorld) -> None:
    from contentshare.gmail.intake import peek

    for i in range(5):
        w.gmail.add(make_message(message_id=f"m{i}", internal_ms=1_793_000_000_000 + i))
    assert len(peek(w.gateway, w.config, limit=3)) == 3

    w.gmail.add(make_message(message_id="odd", internal_ms=1_793_000_009_000))
    for header in w.gmail.messages["odd"]["payload"]["headers"]:
        if header["name"] == "From":
            header["value"] = "anita@dept.org, bob@dept.org"
    rows = peek(w.gateway, w.config, limit=10)
    assert rows[0].message_id == "odd"
    assert rows[0].verdict == "unreadable"

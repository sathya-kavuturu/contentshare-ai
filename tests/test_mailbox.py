"""Reading mail in every folder: only new mail after the first check, automatic mailings and
non-requests are skipped with a reason, and a skipped mail can be moved to the requests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import Qt

from contentshare.app.controller import ActionError
from contentshare.config.models import GmailSettings, SmartSettings
from contentshare.gmail.intake import SCAN_MARKER, IntakeService
from contentshare.gmail.mailrules import automatic_sender, mail_category
from contentshare.smart.schema import Reading
from contentshare.storage.db import MIGRATIONS, Database
from contentshare.ui.main_window import PAGES
from fake_gmail import make_message
from helpers import TODAY, MailWorld, build_mail_world
from test_controller import Harness, h  # noqa: F401  (the controller fixture)
from test_smart import EXISTING, STRANGER, FakeReader
from test_ui import Rig, rig  # noqa: F401  (the window fixture)

ASK = "Kindly share the recordings for us."


@pytest.fixture
def world(tmp_path: Path, db: Database) -> MailWorld:
    return build_mail_world(tmp_path, db)


def service(w: MailWorld, reader: Any = None, **smart: Any) -> IntakeService:
    config = w.config.model_copy(update={"smart": SmartSettings(enabled=True, **smart)})
    return IntakeService(w.gateway, w.repo, w.index, config, today=lambda: TODAY, reader=reader)


def stranger(w: MailWorld, body: str, message_id: str = "u1", **kw: Any) -> None:
    kw.setdefault("subject", "Content please")
    w.gmail.add(make_message(message_id=message_id, from_=STRANGER, body=body, **kw))


# --------------------------------------------------------------------------- the rules
def test_the_gmail_label_group_of_a_mail() -> None:
    assert mail_category(["INBOX", "CATEGORY_PROMOTIONS"]) == "Promotions"
    assert mail_category(["INBOX", "CATEGORY_UPDATES"]) == "Updates"
    assert mail_category(["SPAM"]) == "Spam"
    assert mail_category(["INBOX"]) == "Primary"
    assert mail_category(["Label_1"]) == "Other"


def test_automatic_addresses_are_recognised() -> None:
    for address in ("noreply@x.org", "no-reply@x.org", "do_not_reply@x.org", "mailer-daemon@x.org"):
        assert automatic_sender(address)
    assert not automatic_sender("ravi@x.org") and not automatic_sender("replyto@x.org")


def test_legacy_lookback_setting_is_dropped_and_first_check_defaults_to_five_days() -> None:
    assert GmailSettings.model_validate({"lookback_days": 30}).first_scan_days == 5


def test_the_database_has_the_mailbox_tables(db: Database) -> None:
    assert len(MIGRATIONS) >= 8
    with db.read() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        columns = {r[1] for r in conn.execute("PRAGMA table_info(emails)")}
    assert {"scan_state", "sender_rules"} <= tables and "labels" in columns


# ---------------------------------------------------------------- the first and later checks
def test_first_check_reads_five_days_and_later_checks_only_what_is_new(world: MailWorld) -> None:
    stranger(world, ASK)
    reader = FakeReader(Reading(False, reason="newsletter"))
    sv = service(world, reader)
    assert world.repo.scan_value(SCAN_MARKER) is None
    first = sv.check_now()
    assert first.first_check and first.examined == 1
    assert any("newer_than:5d" in q for q in world.gmail.queries)
    marker = world.repo.scan_value(SCAN_MARKER)
    assert marker is not None

    world.gmail.queries.clear()
    fetched = world.gmail.calls.count("messages.get")
    second = sv.check_now()
    assert not second.first_check and second.examined == 0
    assert all("newer_than" not in q and "after:" in q for q in world.gmail.queries)
    assert world.gmail.calls.count("messages.get") == fetched  # the seen mail is not read again
    stamp = int((datetime.fromisoformat(marker) - timedelta(days=1)).timestamp())
    assert any(f"after:{stamp}" in q for q in world.gmail.queries)


def test_spam_and_promotions_are_read_too(world: MailWorld) -> None:
    stranger(world, ASK, "p1", labels=("INBOX", "CATEGORY_PROMOTIONS"))
    stranger(world, ASK, "s1", labels=("SPAM",))
    reader = FakeReader(Reading(False, reason="not about content"))
    report = service(world, reader).check_now()
    assert report.examined == 2 and reader.calls == 2  # the text of both was judged
    assert all(world.gmail.spam_flags)
    labels = {r["gmail_message_id"]: r["labels"] for r in world.repo.ignored_emails()}
    assert "CATEGORY_PROMOTIONS" in labels["p1"] and "SPAM" in labels["s1"]


def test_the_next_check_starts_from_the_old_place_while_mail_is_still_waiting(
    world: MailWorld,
) -> None:
    stranger(world, ASK)
    waiting = service(world, FakeReader(Reading(True), available=False)).check_now()
    assert waiting.deferred == 1
    assert world.repo.scan_value(SCAN_MARKER) is None  # not moved past mail that is not done
    done = service(world, FakeReader(Reading(False, reason="no"))).check_now()
    assert done.deferred == 0 and world.repo.scan_value(SCAN_MARKER) is not None


def test_a_stopped_check_does_not_move_the_starting_place(world: MailWorld) -> None:
    stranger(world, ASK)
    service(world, FakeReader(Reading(False))).check_now(should_stop=lambda: True)
    assert world.repo.scan_value(SCAN_MARKER) is None


def test_more_mail_than_one_check_reads_continues_next_time(world: MailWorld) -> None:
    for n in range(3):
        stranger(world, ASK, f"u{n}")
    world.rebuild(max_messages=2)
    sv = service(world, FakeReader(Reading(False)))
    sv.config = world.config.model_copy(update={"smart": SmartSettings(enabled=True)})
    assert sv.check_now().examined == 2 and world.repo.scan_value(SCAN_MARKER) is None
    assert sv.check_now().examined == 1 and world.repo.scan_value(SCAN_MARKER) is not None


# ---------------------------------------------------------------- what is skipped, and why
def test_a_mail_the_model_reads_as_no_request_is_skipped_with_its_reason(world: MailWorld) -> None:
    stranger(world, f"Weekly newsletter. {EXISTING} was great, thanks!")
    reader = FakeReader(Reading(False, (), 0.8, "a newsletter"))
    report = service(world, reader).check_now()
    assert report.requests == 0 and report.ignored == 1 and reader.calls == 1
    (row,) = world.repo.ignored_emails()
    assert row["ignored_reason"].endswith("a newsletter")
    assert "newsletter" in row["snippet"]  # a short start is kept to look at it


def test_the_model_is_asked_before_a_loose_match_in_the_index(world: MailWorld) -> None:
    stranger(world, "Thank you for sending the YouthRetreat recording, it was wonderful")
    reader = FakeReader(Reading(False, (), 0.9, "a thank-you"))
    report = service(world, reader).check_now()
    assert reader.calls == 1 and report.requests == 0  # before: the index match made a request


def test_an_unsure_mail_counts_as_skipped(world: MailWorld) -> None:
    stranger(world, ASK)
    report = service(world, FakeReader(Reading(False, (), 0.2, ""))).check_now()
    assert report.ignored == 1 and report.requests == 0


# --------------------------------------------------------------------------- the person's choices
def test_a_skipped_mail_can_be_moved_to_the_requests(world: MailWorld) -> None:
    stranger(world, ASK, subject="Pack please")
    sv = service(world, FakeReader(Reading(False, (), 0.9, "unclear")))
    sv.check_now()
    assert world.repo.mailbox()[0]["lines"] == 0
    ids = sv.move_to_requests("u1")
    assert len(ids) == 1
    (row,) = world.repo.mailbox()
    assert row["ignored_reason"] is None and row["lines"] == 1 and row["department"] is None
    assert sv.move_to_requests("u1") == []  # already a request: nothing more to do
    (request,) = world.repo.list_requests()
    assert request["state"] == "NEEDS_REVIEW"  # the folders are added by the person


def test_a_skipped_mail_with_names_in_it_becomes_a_reviewable_request(world: MailWorld) -> None:
    stranger(world, f"Hello\n{EXISTING}\nThanks")
    world.repo.add_email(
        gmail_message_id="u1", gmail_thread_id="t", sender_email="ravi@elsewhere.com",
        received_at="2026-11-01T00:00:00+00:00", auth_status="unknown",
        ignored_reason="not recognised as a content request",
    )  # fmt: skip
    ids = service(world, FakeReader(Reading(False))).move_to_requests("u1")
    (request,) = world.repo.list_requests()
    assert ids == [request["id"]] and EXISTING in request["query_json"]


def test_reading_a_skipped_mail_again_asks_the_model_once_more(world: MailWorld) -> None:
    stranger(world, ASK)
    service(world, FakeReader(Reading(False, (), 0.9, "no"))).check_now()
    again = service(world, FakeReader(Reading(True, (EXISTING,), 0.9))).read_again("u1")
    assert again.requests == 1
    assert world.repo.mailbox()[0]["lines"] == 1


def test_reading_again_with_no_model_keeps_the_mail_in_the_mailbox(world: MailWorld) -> None:
    stranger(world, ASK)
    service(world, FakeReader(Reading(False, (), 0.9, "no"))).check_now()
    service(world, FakeReader(Reading(True), available=False)).read_again("u1")
    (row,) = world.repo.ignored_emails()
    assert "could not be read again" in row["ignored_reason"]


def test_always_skip_and_always_accept_a_sender(world: MailWorld) -> None:
    world.repo.set_sender_rule("ravi@elsewhere.com", "skip")
    stranger(world, ASK)
    reader = FakeReader(Reading(True, (EXISTING,), 0.9))
    report = service(world, reader).check_now()
    assert reader.calls == 0 and report.ignored == 1
    (row,) = world.repo.ignored_emails()
    assert "always skip" in row["ignored_reason"]

    world.repo.set_sender_rule("ravi@elsewhere.com", "request")
    stranger(world, ASK, "u2")
    report = service(world, reader).check_now()
    assert reader.calls == 0 and report.requests == 1  # no judging for an accepted sender
    world.repo.set_sender_rule("ravi@elsewhere.com", None)
    assert world.repo.sender_rule("ravi@elsewhere.com") is None


def test_the_mailbox_lists_requests_and_skipped_mail_with_their_labels(world: MailWorld) -> None:
    stranger(world, f"Hello\n{EXISTING}\nThanks", "r1")
    stranger(world, ASK, "p1", labels=("INBOX", "CATEGORY_PROMOTIONS"))
    service(world, FakeReader(Reading(False, (), 0.9, "an offer"))).check_now()
    rows = {r["gmail_message_id"]: r for r in world.repo.mailbox()}
    assert rows["r1"]["lines"] == 1 and rows["p1"]["lines"] == 0
    assert "CATEGORY_PROMOTIONS" in rows["p1"]["labels"] and "INBOX" in rows["r1"]["labels"]


# --------------------------------------------------------------------------- through the controller
def test_the_controller_moves_skipped_mail_and_remembers_the_choice(h: Harness) -> None:  # noqa: F811
    h.world.gmail.add(make_message(message_id="c1", from_=STRANGER, subject="Hi", body=ASK))
    h.ctl.config = h.ctl.config.model_copy(update={"smart": SmartSettings(enabled=False)})
    h.ctl.check_now()
    (row,) = h.ctl.mailbox()
    assert row["lines"] == 0
    made = h.ctl.move_to_requests([row["id"]])
    assert len(made) == 1 and h.ctl.mailbox()[0]["lines"] == 1
    assert h.ctl.move_to_requests([row["id"]]) == []  # not skipped any more
    h.ctl.always_skip_sender("ravi@elsewhere.com")
    assert h.ctl.sender_rule("ravi@elsewhere.com") == "skip"
    h.ctl.always_accept_sender("ravi@elsewhere.com")
    assert h.ctl.sender_rule("ravi@elsewhere.com") == "request"


def test_moving_mail_is_refused_while_a_check_runs(h: Harness) -> None:  # noqa: F811
    assert h.ctl._check_lock.acquire(blocking=False)
    try:
        with pytest.raises(ActionError, match="check is running"):
            h.ctl.move_to_requests([1])
    finally:
        h.ctl._check_lock.release()


def test_forgetting_all_skipped_mail_starts_the_first_check_again(h: Harness) -> None:  # noqa: F811
    h.world.gmail.add(make_message(message_id="own", labels=("INBOX", "SENT")))
    h.ctl.check_now()
    assert h.ctl.repo.scan_value(SCAN_MARKER) is not None
    assert h.ctl.forget_all_skipped() == 1
    assert h.ctl.repo.scan_value(SCAN_MARKER) is None
    assert json.loads(json.dumps(h.ctl.check_now().request_ids)) == []


# --------------------------------------------------------------------------- the Mailbox screen
def test_the_mailbox_screen_sorts_and_filters_by_label_and_result(rig: Rig) -> None:  # noqa: F811
    repo = rig.controller.repo
    for n, (labels, stamp) in enumerate(
        [
            ("INBOX CATEGORY_PROMOTIONS", "2026-11-02"),
            ("SPAM", "2026-11-05"),
            ("INBOX", "2026-11-03"),
        ]
    ):
        repo.add_email(
            gmail_message_id=f"x{n}", gmail_thread_id=f"t{n}", sender_email=f"a{n}@y.org",
            received_at=f"{stamp}T10:00:00+00:00", auth_status="unknown", subject=f"Subject {n}",
            ignored_reason="not recognised as a content request", labels=labels,
        )  # fmt: skip
    window = rig.window
    window.nav.setCurrentRow(PAGES.index("Mailbox"))
    view = window.mailbox.view
    assert view.table.rowCount() == 3
    first = view.table.item(0, 2)
    assert first is not None and first.text() == "Subject 1"  # newest first by default
    view.table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
    oldest = view.table.item(0, 2)
    assert oldest is not None and oldest.text() == "Subject 0"
    view.label.setCurrentIndex(view.label.findData("Spam"))
    assert view.table.rowCount() == 1
    view.label.setCurrentIndex(0)
    view.result.setCurrentIndex(view.result.findData("Request"))
    assert view.table.rowCount() == 0  # all three were left alone


def test_moving_mail_shows_a_message_and_keeps_the_window_free(rig: Rig) -> None:  # noqa: F811
    from contentshare.ui.mail_view import MailView

    jobs: list[Any] = []

    class LaterBridge:
        def work(self, job: Any, done: Any, failed: Any) -> None:
            jobs.append((job, done, failed))  # the job runs "in the background" later

    rig.controller.repo.add_email(
        gmail_message_id="b1", gmail_thread_id="t", sender_email="a@y.org",
        received_at="2026-11-05T10:00:00+00:00", auth_status="unknown", subject="Pack",
        ignored_reason="not recognised as a content request",
    )  # fmt: skip
    view = MailView(rig.controller, bridge=LaterBridge())  # type: ignore[arg-type]
    view.table.selectRow(0)
    view.move_to_requests()
    assert "Moving 1 mail(s)" in view.status.text() and not view.status.isHidden()
    assert not view.move_button.isEnabled() and not view.again.isEnabled()  # no double click
    job, done, _failed = jobs[0]
    done("1 mail(s) moved to the Requests (1 line(s)); add the folders there")
    assert "moved to the Requests" in view.status.text() and job is not None

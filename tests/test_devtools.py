"""Developer mode: the trace of a mail check, the Gmail call log, the test mailbox, the switches,
the tools of the Developer screen, and that secrets and mail text stay out of what is copied."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from contentshare.config.models import SmartSettings
from contentshare.devtools import tools
from contentshare.devtools.gateways import FakeGateway, NoSendGateway
from contentshare.devtools.logbuffer import BUFFER, attach, detach
from contentshare.devtools.trace import TRACE
from contentshare.gmail.intake import IntakeService
from contentshare.smart.schema import Reading
from contentshare.storage.db import Database
from fake_gmail import make_message
from helpers import TODAY, MailWorld, build_mail_world
from test_controller import Harness, h  # noqa: F401  (the controller fixture)
from test_folders_help import Rig, rig  # noqa: F401  (the window fixture)

EXISTING = "YouthRetreat_2026-09-04_Day2"
STRANGER = "Ravi Kumar <ravi@elsewhere.com>"


@pytest.fixture(autouse=True)
def _clean_trace() -> Iterator[None]:
    TRACE.clear()
    BUFFER.clear()
    yield
    TRACE.enabled = False
    TRACE.clear()
    detach()


@pytest.fixture
def world(tmp_path: Path, db: Database) -> MailWorld:
    return build_mail_world(tmp_path, db)


def stranger(w: MailWorld, body: str, message_id: str = "u1") -> None:
    w.gmail.add(
        make_message(message_id=message_id, from_=STRANGER, subject="Content please", body=body)
    )


def intake(w: MailWorld, **smart: Any) -> IntakeService:
    config = w.config.model_copy(update={"smart": SmartSettings(**smart)})
    return IntakeService(w.gateway, w.repo, w.index, config, today=lambda: TODAY)


# --------------------------------------------------------------------------- the trace
def test_nothing_is_recorded_while_developer_mode_is_off(world: MailWorld) -> None:
    stranger(world, f"Hello\n{EXISTING}\nThanks")
    intake(world).check_now()
    assert TRACE.last_check() is None and not TRACE.call_lines()


def test_a_check_leaves_the_search_the_ids_and_the_decision(world: MailWorld) -> None:
    TRACE.enabled = True
    stranger(world, f"Hello\n{EXISTING}\nThanks")
    stranger(world, "Just saying hello, hope you are well", message_id="u2")
    intake(world).check_now()
    last = TRACE.last_check()
    assert last is not None
    text = last.text()
    assert "Gmail search: in:inbox category:primary" in text  # the exact query
    assert "u1" in text and "u2" in text  # the ids Gmail returned
    assert "Department of the sender: none" in text
    assert "something on the server matches" in text  # the first mail
    assert "nothing matches" in text  # the second
    assert "request #" in text and "left alone: sender is not in any department" in text
    assert "Result: 2 read, 1 request(s)" in text


def test_mail_seen_before_is_listed_as_not_fetched_again(world: MailWorld) -> None:
    TRACE.enabled = True
    world.rebuild(apply_label=False)  # Gmail then returns the mail again; the program skips it
    world.gmail.add(
        make_message(message_id="d1", subject="Hello", body="Kindly share nothing in particular")
    )
    world.intake.check_now()
    world.intake.check_now()
    last = TRACE.last_check()
    assert last is not None and "Already looked at before" in last.text()


def test_the_gmail_calls_are_logged_with_the_time_taken(world: MailWorld) -> None:
    TRACE.enabled = True
    stranger(world, "Please share the recording")
    intake(world).check_now()
    lines = "\n".join(TRACE.call_lines())
    assert "messages.list" in lines and "messages.get" in lines and " ms " in lines


def test_a_failing_call_is_logged_as_failed(world: MailWorld) -> None:
    from fake_gmail import http_error

    TRACE.enabled = True
    world.gmail.failures["messages.list"] = [http_error(404, "notFound")]
    report = world.intake.check_now()
    assert report.errors
    assert any("FAILED" in line for line in TRACE.call_lines())


def test_the_model_answer_is_part_of_the_trace(world: MailWorld) -> None:
    class FakeReader:
        def available(self) -> bool:
            return True

        def read(self, subject: str, body: str) -> Reading:
            return Reading(True, ("Zebra Cove Talk",), 0.9, "asked", answer='{"is_request": true}')

    TRACE.enabled = True
    stranger(world, "Kindly share the Zebra Cove Talk")
    config = world.config.model_copy(update={"smart": SmartSettings(enabled=True)})
    IntakeService(
        world.gateway, world.repo, world.index, config, today=lambda: TODAY, reader=FakeReader()
    ).check_now()
    last = TRACE.last_check()
    assert last is not None
    text = last.text()
    assert "Asking the local model" in text and "Local model: a content request" in text
    assert 'Model answer as sent: {"is_request": true}' in text


# --------------------------------------------------------------------------- the log
def test_the_log_buffer_keeps_lines_and_can_be_filtered() -> None:
    attach(debug=False)
    log = logging.getLogger("contentshare.test")
    log.setLevel(logging.INFO)
    log.warning("something to find")
    log.info("something else")
    assert any("something to find" in line for line in BUFFER.lines(logging.WARNING))
    assert not any("something else" in line for line in BUFFER.lines(logging.WARNING))
    assert BUFFER.lines(contains="ELSE")  # the search ignores case


def test_a_log_line_never_carries_an_exception_message() -> None:
    attach(debug=False)
    try:
        raise ValueError("secret mail text")
    except ValueError:
        logging.getLogger("contentshare.test").exception("failed")
    assert not any("secret mail text" in line for line in BUFFER.lines())


# --------------------------------------------------------------------------- the switches
def test_developer_switches_apply_at_once_and_are_saved(h: Harness) -> None:  # noqa: F811
    ctl = h.ctl
    assert not TRACE.enabled
    ctl.set_dev_option("enabled", True)
    assert TRACE.enabled and ctl.config.dev.enabled
    ctl.set_dev_option("debug_log", True)
    assert logging.getLogger().level == logging.DEBUG
    ctl.set_dev_option("debug_log", False)
    ctl.set_dev_option("enabled", False)
    assert not TRACE.enabled
    with pytest.raises(ValueError):
        ctl.set_dev_option("nonsense", True)


def test_the_test_mailbox_is_used_instead_of_gmail(h: Harness) -> None:  # noqa: F811
    ctl = h.ctl
    ctl.set_dev_option("enabled", True)
    ctl.set_dev_option("fake_gmail", True)
    report = ctl.check_now()
    assert report.examined == 5  # one department mail, two strangers, a newsletter, a greeting
    assert report.requests >= 1
    last = TRACE.last_check()
    assert last is not None and "dev-stranger-polite" in last.text()


def test_no_replies_stops_only_the_sending() -> None:
    class Real:
        sent = 0

        def profile_email(self) -> str:
            return "me@example.org"

        def send_raw(self, raw: str, thread: str | None) -> str:
            Real.sent += 1
            return "x"

    wrapped = NoSendGateway(Real())
    assert wrapped.profile_email() == "me@example.org"  # reading goes through
    assert wrapped.send_raw("raw", None) == "dev-not-sent" and Real.sent == 0


def test_the_fake_mailbox_has_every_kind_of_mail() -> None:
    gateway = FakeGateway("anita@dept.org")
    ids = gateway.list_message_ids("anything", 50)
    assert len(ids) == 5
    gateway.add_label(ids[0], gateway.ensure_label("x"))
    assert len(gateway.list_message_ids("anything", 50)) == 4  # a labelled mail is not listed


def test_skipped_mail_can_all_be_read_again(h: Harness) -> None:  # noqa: F811
    h.world.gmail.add(make_message(message_id="own", labels=("INBOX", "SENT")))
    h.ctl.check_now()
    assert len(h.ctl.repo.ignored_emails()) == 1  # "sent by this account": remembered for good
    assert h.ctl.check_now().examined == 0
    assert h.ctl.forget_all_skipped() == 1
    assert h.ctl.check_now().examined == 1  # read again


# --------------------------------------------------------------------------- the tools
def test_explain_shows_each_decision_for_a_stranger(h: Harness) -> None:  # noqa: F811
    text = tools.explain_email(
        h.ctl, "someone@example.test", "Content please", f"Hello\n{EXISTING}\nThanks"
    )
    assert "The sender is in no department" in text
    assert "Counts as found in the index" in text and "YES" in text
    assert "without asking the model" in text


def test_explain_says_when_nothing_is_found_and_smart_reading_is_off(h: Harness) -> None:  # noqa: F811
    text = tools.explain_email(h.ctl, "someone@example.test", "Hi", "Hello, how are you today")
    assert "NO" in text and "left alone" in text


def test_explain_knows_a_department_sender(h: Harness) -> None:  # noqa: F811
    text = tools.explain_email(h.ctl, "anita@dept.org", "Hi", "Please share the Sunrise Talk")
    assert "department 'Media Team'" in text


def test_the_index_search_shows_scores(h: Harness) -> None:  # noqa: F811
    text = tools.index_search(h.ctl, "Sunrise Talk")
    assert "%" in text and "SunriseTalk" in text


def test_the_index_and_model_summaries_work(h: Harness) -> None:  # noqa: F811
    assert "Items in the index" in tools.index_summary(h.ctl)
    assert "Smart reading: off" in tools.model_status(h.ctl)


def test_a_gmail_search_shows_the_raw_results(h: Harness) -> None:  # noqa: F811
    h.world.gmail.add(make_message(message_id="q1", from_="Lee <lee@x.org>", subject="Found me"))
    text = tools.run_gmail_query(h.ctl, "in:inbox", 5)
    assert "q1" in text and "lee@x.org" in text and "NEW" in text


def test_the_database_viewer_shows_allowed_tables_and_nothing_else(h: Harness) -> None:  # noqa: F811
    columns, _rows = tools.table_rows(h.ctl, "emails")
    assert "sender_email" in columns
    with pytest.raises(ValueError):
        tools.table_rows(h.ctl, "users")  # holds password hashes
    with pytest.raises(ValueError):
        tools.table_rows(h.ctl, "emails; DROP TABLE emails")


def test_the_copied_diagnostics_hold_no_secret_and_hide_mail_by_default(h: Harness) -> None:  # noqa: F811
    TRACE.enabled = True
    h.world.gmail.add(
        make_message(message_id="s1", from_=STRANGER, subject="A very private subject", body="x")
    )
    h.ctl.check_now()
    quiet = tools.diagnostics(h.ctl)
    assert "A very private subject" not in quiet and "ravi@elsewhere.com" not in quiet
    assert "ContentShare AI" in quiet and "== Last mail check ==" in quiet
    assert "client_secret" not in quiet.replace("client_secret_path", "")
    full = tools.diagnostics(h.ctl, include_mail_text=True)
    assert "A very private subject" in full


# --------------------------------------------------------------------------- the screen
def test_the_developer_screen_is_in_the_menu_only_while_the_mode_is_on(rig: Rig) -> None:  # noqa: F811
    window = rig.window
    row = window.nav.count() - 1
    assert window.nav.item(row).text() == "Developer" and window.nav.item(row).isHidden()
    assert not window.dev_banner.isVisible()
    rig.controller.set_dev_option("enabled", True)
    window.update_chrome()
    assert not window.nav.item(row).isHidden() and not window.dev_banner.isHidden()
    window.nav.setCurrentRow(row)
    assert window.stack.currentWidget() is window.developer
    rig.controller.set_dev_option("enabled", False)
    window.update_chrome()
    assert window.nav.item(row).isHidden()
    assert window.stack.currentWidget() is not window.developer  # sent back to Settings


def test_the_settings_checkbox_turns_the_mode_on(rig: Rig) -> None:  # noqa: F811
    page = rig.window.settings
    assert not page.collect().dev.enabled
    page.dev_mode.setChecked(True)
    assert page.collect().dev.enabled


def test_every_tab_of_the_developer_screen_fills_without_error(rig: Rig) -> None:  # noqa: F811
    rig.controller.set_dev_option("enabled", True)
    page = rig.window.developer
    for index in range(page.tabs.count()):
        page.tabs.setCurrentIndex(index)
        page.refresh()
    page.tabs.setCurrentWidget(page.db_tab)
    assert page.db_table.columnCount() > 0
    page.t_sender.setText("someone@example.test")
    page.t_body.setPlainText(f"Hello\n{EXISTING}\nThanks")
    page.explain_button.click()
    TRACE.enabled = True
    assert page.check_button.isEnabled()

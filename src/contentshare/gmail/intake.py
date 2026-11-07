"""Checking Gmail for new requests.

Mail from senders in a department is always looked at; mail from anyone else too when smart
reading is on (it is then read in every Gmail folder, Promotions and Spam included, and the
local model decides whether it asks for content). Every message is checked again after it is
read (a Gmail search is a convenience, not a security control). Each message is processed once:
its Gmail id is recorded in the database, and optionally a label is added in Gmail.

The first check reads back a few days (Settings); after that only mail that arrived since the
last complete check is listed, and a mail already seen is never fetched again. One bad message
never stops the others, and nothing about a message's content is logged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from contentshare.config.models import AppConfig, Department
from contentshare.devtools.trace import TRACE
from contentshare.gmail.errors import AuthRequiredError, GmailError, MessageFormatError
from contentshare.gmail.gateway import GmailGateway
from contentshare.gmail.mailrules import plainly_automatic
from contentshare.gmail.message import InboundEmail, parse_message
from contentshare.gmail.sender_auth import verify_sender
from contentshare.index import ContentIndex
from contentshare.jobs.states import RequestState
from contentshare.matching import process_email_requests, process_named_requests
from contentshare.matching.service import exact_items, search_found
from contentshare.parsing.request import parse_request
from contentshare.parsing.text import clean_body
from contentshare.smart.runtime import SmartError
from contentshare.smart.schema import Reading, reading_summary
from contentshare.storage.repository import Repository

log = logging.getLogger(__name__)

SENDERS_PER_QUERY = 15
# Words that make "Thanks, received!" look like a program name. Ignored only when deciding
# whether a reply in a handled conversation contains a new request.
ACKNOWLEDGEMENT_WORDS = (
    "received", "noted", "ok", "okay", "got", "great", "perfect", "awesome", "wonderful",
    "excellent", "appreciate", "appreciated", "acknowledged", "confirmed", "confirm", "done",
    "much", "very", "yes", "sure", "nice", "fine",
)  # fmt: skip
UNREADABLE_SENDER = "unreadable@invalid.example"
NOT_A_REQUEST = "not recognised as a content request"
NOT_IN_DEPARTMENT = "sender is not in any department"
SCAN_MARKER = "mail_checked_until"  # when the last complete check began (ISO, UTC)
SCAN_OVERLAP = timedelta(days=1)  # listed again on purpose; the database stops any second read
LIST_CAP = 500  # message ids listed per search (cheap: ids only)
# Everywhere in the mailbox, Promotions and Spam included, except what is not mail to read.
# (The API leaves Spam out unless asked, see ``include_spam_trash``.)
EVERYWHERE = "-in:trash -in:sent -in:drafts -in:chats"
# A listed sender's mail is left alone only when the model is at least this sure it is no request.
JUDGE_SURE = 0.6
SKIPPED_SNIPPET = 600  # characters of a skipped mail kept for the Mailbox


def _not_a_request(reading: Reading) -> str:
    """Why a mail was skipped, in the model's words when it gave any."""
    return f"{NOT_A_REQUEST}: {reading.reason}" if reading.reason else NOT_A_REQUEST


class Reader(Protocol):
    """Reads one email with the language model (see ``contentshare.smart``)."""

    def available(self) -> bool: ...

    def read(self, subject: str, body: str) -> Reading: ...


@dataclass
class IntakeReport:
    examined: int = 0  # new messages read this time
    requests: int = 0  # request lines created
    ready: int = 0
    needs_review: int = 0
    ignored: int = 0
    errors: list[str] = field(default_factory=list)  # "message <id>: <kind>", never content
    warnings: list[str] = field(default_factory=list)
    smart_read: int = 0  # emails from unlisted senders the language model read
    smart_requests: int = 0  # of those, how many were found to ask for content
    needs_sign_in: bool = False
    stopped: bool = False
    already_running: bool = False  # another check held the mailbox; this one did nothing
    request_ids: list[int] = field(default_factory=list)
    deferred: int = 0  # mail not decided now (model busy or missing): it is read again next check
    first_check: bool = False  # nothing was checked before, so a few days back were read


def label_search_name(label: str) -> str:
    """A label as Gmail search spells it: ``My/Label`` becomes ``my-label``."""
    return label.lower().replace("/", "-").replace(" ", "-")


def window_clause(config: AppConfig, since: datetime | None) -> str:
    """The time part of a search: the first check goes back ``first_scan_days``; every later one
    starts a little before the last complete check (so nothing in between is missed)."""
    if since is None:
        return f"newer_than:{config.gmail.first_scan_days}d"
    return f"after:{int((since - SCAN_OVERLAP).timestamp())}"


def build_queries(config: AppConfig, since: datetime | None = None) -> list[str]:
    """Gmail searches that can only return mail from the configured senders. Senders are
    validated addresses and domains, so nothing in them can change the meaning of a query."""
    senders = sorted(
        {e for d in config.departments for e in d.sender_emails}
        | {dom for d in config.departments for dom in d.sender_domains}
    )
    suffix = f" -in:trash {window_clause(config, since)}"
    if config.gmail.apply_label:
        suffix += f" -label:{label_search_name(config.gmail.processed_label)}"
    return [
        f"from:({' OR '.join(senders[i : i + SENDERS_PER_QUERY])}){suffix}"
        for i in range(0, len(senders), SENDERS_PER_QUERY)
    ]


def build_unlisted_query(config: AppConfig, since: datetime | None = None) -> str:
    """Recent mail from anyone, in every folder (used only when smart reading is on). Mail sent
    by this account is left out; automatic mailings are recognised after they are read."""
    # No "processed" label filter here: mail left alone by an earlier version carries that label,
    # and Gmail would hide it for good. The database already stops any message being done twice.
    return f"{EVERYWHERE} -from:me {window_clause(config, since)}"


class IntakeService:
    def __init__(
        self,
        gateway: GmailGateway,
        repo: Repository,
        index: ContentIndex,
        config: AppConfig,
        *,
        today: Callable[[], date] = date.today,
        reader: Reader | None = None,
    ) -> None:
        self.gateway, self.repo, self.index, self.config = gateway, repo, index, config
        self._today = today
        self._reader = reader
        self._label_id: str | None = None

    # ------------------------------------------------------------------ public
    def check_now(
        self,
        *,
        refresh_index: bool = False,
        should_stop: Callable[[], bool] | None = None,
    ) -> IntakeReport:
        report = IntakeReport()
        TRACE.begin_check()
        started = datetime.now(UTC)
        if refresh_index:
            self._refresh_index(report)
        try:
            ids, truncated = self._new_ids(report)
        except AuthRequiredError:
            report.needs_sign_in = True
            TRACE.end_check("Gmail asked to sign in again")
            return report
        except GmailError as exc:
            report.errors.append(f"could not list mail: {type(exc).__name__}")
            TRACE.end_check(f"could not list mail: {exc}")
            return report

        for message_id in ids:
            if should_stop and should_stop():
                report.stopped = True
                break
            try:
                self._handle(message_id, report)
            except AuthRequiredError:
                report.needs_sign_in = True
                break
            except MessageFormatError as exc:
                self._record_unreadable(message_id, str(exc), report)
            except GmailError as exc:
                report.errors.append(f"message {message_id}: {type(exc).__name__}")
            except Exception as exc:
                log.exception("unexpected error handling message %s", message_id)
                report.errors.append(f"message {message_id}: {type(exc).__name__}")
        # The next check starts from here only when everything listed was dealt with; otherwise
        # it starts from the old place again (mail already seen is skipped by its id).
        if not (
            truncated or report.stopped or report.deferred or report.errors or report.needs_sign_in
        ):
            self.repo.set_scan_value(SCAN_MARKER, started.isoformat(timespec="seconds"))
        self.repo.log(
            "system", "mail_checked", None, examined=report.examined, requests=report.requests,
            ready=report.ready, review=report.needs_review, ignored=report.ignored,
            errors=len(report.errors),
        )  # fmt: skip
        extra = [f"Warning: {w}" for w in report.warnings] + [f"Error: {e}" for e in report.errors]
        TRACE.end_check(
            "\n".join(
                [
                    f"Result: {report.examined} read, {report.requests} request(s) "
                    f"({report.ready} ready, {report.needs_review} to review), "
                    f"{report.ignored} left alone, {len(report.errors)} error(s)",
                    *extra,
                ]
            )
        )
        return report

    def move_to_requests(self, gmail_message_id: str) -> list[int]:
        """The person says a skipped mail is a request. It is fetched again, becomes a request
        (the usual reading of its text; with no names found it is one empty line to add folders
        to), and no model is asked. Returns the request numbers; empty if it already was one."""
        row = self.repo.get_email_by_gmail_id(gmail_message_id)
        if row is None or row["ignored_reason"] is None:
            return []
        msg = parse_message(self.gateway.get_message(gmail_message_id))
        dept = self.config.department_for_sender(msg.sender_email)
        settings = self.config.gmail
        auth = verify_sender(
            msg.auth_results,
            msg.sender_domain,
            trusted_servers=settings.trusted_authserv_ids,
            require=settings.require_authentication,
        )
        email_id = int(row["id"])
        if not self.repo.convert_ignored_email(
            email_id,
            department=dept.name if dept else None,
            snippet=clean_body(msg.body),
            auth_status=auth.verdict.value,
        ):
            return []
        ids = process_email_requests(
            self.repo,
            self.index,
            self.config,
            email_id,
            msg.subject,
            msg.body,
            today=self._today(),
        )
        report = IntakeReport()
        self._count(ids, report, gmail_message_id)
        self._label(gmail_message_id, report)
        return ids

    def read_again(self, gmail_message_id: str) -> IntakeReport:
        """Read one skipped mail again now, as if it had just arrived (the model is asked)."""
        report = IntakeReport()
        row = self.repo.get_email_by_gmail_id(gmail_message_id)
        if row is None or not self.repo.forget_ignored_email(int(row["id"])):
            return report
        self._handle(gmail_message_id, report)
        if not self.repo.is_processed(gmail_message_id):
            # nothing could decide it now (no model, or it was busy): keep it in the Mailbox
            msg = parse_message(self.gateway.get_message(gmail_message_id))
            self._ignore(
                msg, "could not be read again now (the smart reader is not available)", report
            )
        return report

    # ------------------------------------------------------------------ steps
    def _refresh_index(self, report: IntakeReport) -> None:
        for scan in self.index.scan_all(self.config.all_roots()):
            if scan.status != "complete":
                report.warnings.append(f"Could not refresh the index for {scan.root}: {scan.error}")

    def _since(self) -> datetime | None:
        """When the last complete check began, or ``None`` if there was none yet."""
        text = self.repo.scan_value(SCAN_MARKER)
        try:
            stamp = datetime.fromisoformat(text) if text else None
        except ValueError:
            return None
        if stamp is not None and stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        return stamp

    def _new_ids(self, report: IntakeReport | None = None) -> tuple[list[str], bool]:
        """The mail to look at now: ids not seen before, at most ``max_messages`` of them.
        The flag says there was more than that (the next check continues)."""
        report = report or IntakeReport()
        since = self._since()
        report.first_check = since is None
        TRACE.note(
            f"First check: reading the last {self.config.gmail.first_scan_days} day(s)"
            if since is None
            else f"Reading mail that arrived since {since.isoformat(timespec='minutes')}"
        )
        seen: dict[str, None] = {}
        if self.config.smart.read_unlisted:
            # Mail once left alone only because its sender was in no department (an earlier
            # version, or reading of such senders switched off) is looked at again now.
            TRACE.forgotten(self.repo.forget_ignored_for(NOT_IN_DEPARTMENT))
        queries = build_queries(self.config, since)
        if not queries:
            TRACE.note("No department has a sender, so no department search was made")
        for query in queries:
            found = self.gateway.list_message_ids(query, LIST_CAP, include_spam_trash=True)
            TRACE.search(query, found)
            for message_id in found:
                seen.setdefault(message_id)
        if self.config.smart.read_unlisted:
            unlisted = build_unlisted_query(self.config, since)
            found = self.gateway.list_message_ids(unlisted, LIST_CAP, include_spam_trash=True)
            TRACE.search(unlisted, found)
            for message_id in found:
                seen.setdefault(message_id)
        else:
            TRACE.note("Reading mail from senders in no department is switched off (Settings)")
        fresh = [m for m in seen if not self.repo.is_processed(m)]
        TRACE.seen([m for m in seen if m not in fresh])
        room = self.config.gmail.max_messages
        return fresh[:room], len(fresh) > room

    def _handle(self, message_id: str, report: IntakeReport) -> None:
        raw = self.gateway.get_message(message_id)
        report.examined += 1
        msg = parse_message(raw)
        TRACE.message(msg.message_id, msg.sender_email, msg.subject, list(msg.label_ids))

        if self._screened_out(msg, report):
            return None
        rule = self.repo.sender_rule(msg.sender_email)
        dept = self.config.department_for_sender(msg.sender_email)
        TRACE.step(
            msg.message_id,
            f"Department of the sender: {dept.name if dept else 'none (not in any department)'}",
        )
        if dept is None:
            return self._unlisted_sender(msg, report, accepted=rule == "request")
        if self._is_followup_without_request(msg):
            return self._ignore(msg, "reply in a handled conversation with no new request", report)

        reading = self._department_reading(msg, dept, report)
        if reading is not None and not reading.is_request and reading.confidence >= JUDGE_SURE:
            self._log_reading(reading, [], msg)
            return self._ignore(msg, _not_a_request(reading), report)

        settings = self.config.gmail
        auth = verify_sender(
            msg.auth_results,
            msg.sender_domain,
            trusted_servers=settings.trusted_authserv_ids,
            require=settings.require_authentication,
        )
        email_id = self.repo.add_email(
            gmail_message_id=msg.message_id,
            gmail_thread_id=msg.thread_id,
            sender_email=msg.sender_email,
            sender_name=msg.sender_name,
            department=dept.name,
            subject=msg.subject,
            snippet=clean_body(msg.body)[:2000],
            received_at=msg.received_at,
            auth_status=auth.verdict.value,
            rfc_message_id=msg.rfc_message_id,
            references_header=msg.references,
            labels=" ".join(msg.label_ids),
        )
        if email_id is None:
            return None
        self.repo.log("system", "sender_checked", None, email_id=email_id,
                      verdict=auth.verdict.value, reason=auth.reason)  # fmt: skip
        if reading is not None and dept.use_local_llm and reading.is_request and reading.folders:
            ids = process_named_requests(
                self.repo,
                self.index,
                self.config,
                email_id,
                list(reading.folders),
                today=self._today(),
            )
            report.smart_requests += 1
        else:
            ids = process_email_requests(
                self.repo,
                self.index,
                self.config,
                email_id,
                msg.subject,
                msg.body,
                today=self._today(),
            )
        self._log_reading(reading, ids, msg)
        self._count(ids, report, msg.message_id)
        self._label(msg.message_id, report)
        return None

    def _unlisted_sender(self, msg: InboundEmail, report: IntakeReport, *, accepted: bool) -> None:
        if self.config.smart.read_unlisted or accepted:
            return self._handle_unlisted(msg, report, trusted=accepted)
        return self._ignore(msg, NOT_IN_DEPARTMENT, report)

    def _screened_out(self, msg: InboundEmail, report: IntakeReport) -> bool:
        """Mail that needs no reading at all: sent by this account, or from a sender the person
        said to always skip."""
        if "SENT" in msg.label_ids:
            self._ignore(msg, "sent by this account", report)
            return True
        if self.repo.sender_rule(msg.sender_email) == "skip":
            self._ignore(msg, "you chose to always skip this sender", report)
            return True
        return False

    def _department_reading(
        self, msg: InboundEmail, dept: Department, report: IntakeReport
    ) -> Reading | None:
        """Ask the local model whether a department's email asks for content, unless lines of it
        are exactly folder names. If it cannot answer, the usual reading is used."""
        if not self.config.smart.enabled:
            return None
        found, _notes = exact_items(
            msg.subject, msg.body, self.index, self.config.roots_for(dept), found_only=True
        )
        if found:
            return None
        return self._read_with_model(msg, report)

    def _log_reading(
        self, reading: Reading | None, request_ids: list[int], msg: InboundEmail
    ) -> None:
        """Write what the model answered into the history (names and confidence only)."""
        if reading is None:
            return
        reason = reading_summary(reading)
        if not request_ids:
            self.repo.log("model", "model_read", None, reason=reason, gmail_id=msg.message_id)
        for request_id in request_ids:
            self.repo.log("model", "model_read", request_id, reason=reason)

    # ------------------------------------------------------------------ senders in no department
    def _handle_unlisted(
        self, msg: InboundEmail, report: IntakeReport, *, trusted: bool = False
    ) -> None:
        """An email from someone who is in no department. It becomes a request only if (1) lines
        of it are exactly the names of folders on the server, or (2) the language model reads it
        as a content request. Everything else is kept as "skipped", with the reason, and can be
        moved to the requests by the person. ``trusted``: the person said to always accept this
        sender, so no judging is done. Whatever is found waits for the person: there is no
        department, so nothing can be ready or replied to by itself."""
        if self._is_followup_without_request(msg):
            return self._ignore(msg, "reply in a handled conversation with no new request", report)
        automatic = None if trusted else plainly_automatic(msg)
        if automatic:
            return self._ignore(msg, f"{NOT_A_REQUEST}: {automatic}", report)
        # 1. lines that are exactly folder names on the server
        exact, _notes = exact_items(msg.subject, msg.body, self.index, None, found_only=True)
        TRACE.step(
            msg.message_id,
            "Lines that are exactly folder names: " + (f"{len(exact)} found" if exact else "none"),
        )
        reading: Reading | None = None
        if not (trusted or exact):
            done, reading = self._decide_unlisted(msg, report)
            if done:
                return None
        email_id = self._record_unlisted(msg)
        if email_id is None:
            return None
        if reading is not None:
            ids = process_named_requests(
                self.repo, self.index, self.config, email_id, list(reading.folders),
                today=self._today(),
            )  # fmt: skip
            report.smart_requests += 1
        else:
            ids = process_email_requests(
                self.repo,
                self.index,
                self.config,
                email_id,
                msg.subject,
                msg.body,
                today=self._today(),
            )
        self._log_reading(reading, ids, msg)
        self._count(ids, report, msg.message_id)
        self._label(msg.message_id, report)
        return None

    def _decide_unlisted(
        self, msg: InboundEmail, report: IntakeReport
    ) -> tuple[bool, Reading | None]:
        """Is this mail from an unlisted sender a content request? ``(True, _)``: it was dealt
        with (skipped, or kept for the next check). ``(False, reading)``: make it a request;
        ``reading`` holds the names the model found, or is ``None`` for the plain reading."""
        reader = self._reader
        if not (self.config.smart.enabled and reader is not None and reader.available()):
            return self._decide_without_model(msg, report), None
        # the model reads subject and text and says whether it is a content request
        TRACE.step(msg.message_id, "Asking the local model")
        reading = self._read_with_model(msg, report)
        if reading is None:
            TRACE.outcome(
                msg.message_id, "model could not answer now; will be looked at again next check"
            )
            report.deferred += 1
            return True, None  # the model could not be used now: look at it again next time
        TRACE.step(msg.message_id, reading_summary(reading))
        if reading.answer:
            TRACE.step(msg.message_id, f"Model answer as sent: {reading.answer[:400]}")
        if reading.dropped:
            TRACE.step(
                msg.message_id,
                "Names the model gave that are not in the email (dropped): "
                + "; ".join(reading.dropped),
            )
        if not reading.is_request:
            # not a request by its subject and text; unsure counts as not a request
            self._log_reading(reading, [], msg)
            self._ignore(msg, _not_a_request(reading), report)
            return True, reading
        # a content request by its context: kept even when the index has nothing for the names,
        # so the person can add the folder by hand
        return False, reading

    def _decide_without_model(self, msg: InboundEmail, report: IntakeReport) -> bool:
        """No model to ask: the text is searched in the index. True if the mail was dealt with."""
        found = search_found(msg.subject, msg.body, self.index, self.config, today=self._today())
        TRACE.step(
            msg.message_id,
            "Subject and text searched in the index: "
            + ("something on the server matches" if found else "nothing matches"),
        )
        if found:
            return False
        if not self.config.smart.enabled:
            self._warn_once(
                report,
                "Mail from people in no department only becomes a request when the index has "
                "what it names; turn on Smart reading (Settings) to understand other wording",
            )
            self._ignore(msg, NOT_IN_DEPARTMENT, report)
            return True
        self._read_with_model(msg, report)  # only to say why nothing can be done
        report.deferred += 1
        return True

    def _read_with_model(self, msg: InboundEmail, report: IntakeReport) -> Reading | None:
        reader = self._reader
        if reader is None or not reader.available():
            self._warn_once(
                report,
                "Smart reading is on, but the model is not installed (Settings → Smart reading)",
            )
            return None
        if report.smart_read >= self.config.smart.max_per_check:
            self._warn_once(
                report, "More mail is waiting for the smart reader; it continues next check"
            )
            return None
        report.smart_read += 1
        try:
            return reader.read(msg.subject, msg.body)
        except SmartError as exc:
            self._warn_once(report, f"The smart reader could not read some mail: {exc}")
            return None

    def _record_unlisted(self, msg: InboundEmail) -> int | None:
        settings = self.config.gmail
        auth = verify_sender(
            msg.auth_results,
            msg.sender_domain,
            trusted_servers=settings.trusted_authserv_ids,
            require=settings.require_authentication,
        )
        email_id = self.repo.add_email(
            gmail_message_id=msg.message_id,
            gmail_thread_id=msg.thread_id,
            sender_email=msg.sender_email,
            sender_name=msg.sender_name,
            department=None,
            subject=msg.subject,
            snippet=clean_body(msg.body)[:2000],
            received_at=msg.received_at,
            auth_status=auth.verdict.value,
            rfc_message_id=msg.rfc_message_id,
            references_header=msg.references,
            labels=" ".join(msg.label_ids),
        )
        if email_id is not None:
            self.repo.log("system", "sender_checked", None, email_id=email_id,
                          verdict=auth.verdict.value, reason=auth.reason)  # fmt: skip
        return email_id

    def _is_followup_without_request(self, msg: InboundEmail) -> bool:
        """ "Thanks, received!" in a conversation we already handled is not a new request."""
        if not self.repo.thread_known(msg.thread_id):
            return False
        # Replies keep the original subject, which names the program. Only what the sender
        # wrote this time counts as asking for something.
        chatter = self.config.parsing.model_copy(
            update={"stop_phrases": [*self.config.parsing.stop_phrases, *ACKNOWLEDGEMENT_WORDS]}
        )
        parsed = parse_request("", msg.body, chatter, today=self._today())
        return not any(item.program for item in parsed.items)

    def _ignore(self, msg: InboundEmail, reason: str, report: IntakeReport) -> None:
        """Remember we looked, so it is not fetched again. The mail itself is left untouched.
        A short start of the text is kept so it can be looked at in the Mailbox."""
        self.repo.add_email(
            gmail_message_id=msg.message_id,
            gmail_thread_id=msg.thread_id,
            sender_email=msg.sender_email,
            sender_name=msg.sender_name,
            subject=msg.subject,
            snippet=clean_body(msg.body)[:SKIPPED_SNIPPET],
            received_at=msg.received_at,
            auth_status="unknown",
            ignored_reason=reason,
            labels=" ".join(msg.label_ids),
        )
        report.ignored += 1
        TRACE.outcome(msg.message_id, f"left alone: {reason}")

    def _record_unreadable(self, message_id: str, reason: str, report: IntakeReport) -> None:
        self.repo.add_email(
            gmail_message_id=message_id,
            gmail_thread_id="",
            sender_email=UNREADABLE_SENDER,
            received_at=self._today().isoformat(),
            auth_status="unknown",
            ignored_reason=f"unreadable message: {reason}",
        )
        report.ignored += 1

    def _count(self, request_ids: list[int], report: IntakeReport, message_id: str = "") -> None:
        for request_id in request_ids:
            row = self.repo.get_request(request_id)
            report.requests += 1
            report.request_ids.append(request_id)
            if row:
                TRACE.outcome(message_id, f"request #{request_id}: {row['state']}")
            if row and row["state"] == RequestState.READY:
                report.ready += 1
            else:
                report.needs_review += 1

    def _label(self, message_id: str, report: IntakeReport) -> None:
        """Best effort: the database already records that this message is done."""
        if not self.config.gmail.apply_label:
            return
        try:
            if self._label_id is None:
                self._label_id = self.gateway.ensure_label(self.config.gmail.processed_label)
            self.gateway.add_label(message_id, self._label_id)
        except AuthRequiredError:
            self._warn_once(
                report, "Gmail did not allow adding labels; reconnect Gmail or turn labels off"
            )
        except GmailError as exc:
            self._warn_once(report, f"Could not label some mail ({type(exc).__name__})")

    @staticmethod
    def _warn_once(report: IntakeReport, text: str) -> None:
        if text not in report.warnings:
            report.warnings.append(text)


def summarize(report: IntakeReport) -> dict[str, Any]:
    return {
        "examined": report.examined,
        "requests": report.requests,
        "ready": report.ready,
        "needs_review": report.needs_review,
        "ignored": report.ignored,
        "errors": len(report.errors),
        "needs_sign_in": report.needs_sign_in,
    }


@dataclass(frozen=True)
class PeekRow:
    message_id: str
    sender: str
    department: str | None
    subject: str
    verdict: str
    reason: str


def peek(gateway: GmailGateway, config: AppConfig, limit: int = 10) -> list[PeekRow]:
    """Read-only look at recent mail from the configured senders, showing how each sender
    would be judged. Writes nothing to the database and changes nothing in Gmail, so it is
    safe to run against a real mailbox to see what the authentication headers really say."""
    seen: dict[str, None] = {}
    for query in build_queries(config):
        for message_id in gateway.list_message_ids(query, limit):
            seen.setdefault(message_id)
    rows = []
    for message_id in list(seen)[:limit]:
        try:
            msg = parse_message(gateway.get_message(message_id))
        except MessageFormatError as exc:
            rows.append(PeekRow(message_id, "?", None, "", "unreadable", str(exc)))
            continue
        dept = config.department_for_sender(msg.sender_email)
        auth = verify_sender(
            msg.auth_results,
            msg.sender_domain,
            trusted_servers=config.gmail.trusted_authserv_ids,
            require=config.gmail.require_authentication,
        )
        rows.append(
            PeekRow(
                message_id,
                msg.sender_email,
                dept.name if dept else None,
                msg.subject[:60],
                auth.verdict.value,
                auth.reason,
            )
        )
    return rows

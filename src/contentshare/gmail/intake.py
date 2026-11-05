"""Checking Gmail for new requests.

Only mail from senders in the department list is ever asked for, and every message is checked
again after it is read (a Gmail search is a convenience, not a security control). Each message
is processed once: its Gmail id is recorded in the database, and optionally a label is added in
Gmail. One bad message never stops the others, and nothing about a message's content is logged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

from contentshare.config.models import AppConfig
from contentshare.gmail.errors import AuthRequiredError, GmailError, MessageFormatError
from contentshare.gmail.gateway import GmailGateway
from contentshare.gmail.message import InboundEmail, parse_message
from contentshare.gmail.sender_auth import verify_sender
from contentshare.index import ContentIndex
from contentshare.jobs.states import RequestState
from contentshare.matching import process_email_requests, process_named_requests
from contentshare.matching.service import exact_items
from contentshare.parsing.request import parse_request
from contentshare.parsing.text import clean_body
from contentshare.smart.runtime import SmartError
from contentshare.smart.schema import Reading
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
# Mail from automatic senders is never worth reading for a request.
AUTOMATIC_SENDERS = (
    "noreply OR no-reply OR donotreply OR do-not-reply OR mailer-daemon OR notifications"
)


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


def label_search_name(label: str) -> str:
    """A label as Gmail search spells it: ``My/Label`` becomes ``my-label``."""
    return label.lower().replace("/", "-").replace(" ", "-")


def build_queries(config: AppConfig) -> list[str]:
    """Gmail searches that can only return mail from the configured senders. Senders are
    validated addresses and domains, so nothing in them can change the meaning of a query."""
    senders = sorted(
        {e for d in config.departments for e in d.sender_emails}
        | {dom for d in config.departments for dom in d.sender_domains}
    )
    suffix = f" newer_than:{config.gmail.lookback_days}d"
    if config.gmail.apply_label:
        suffix += f" -label:{label_search_name(config.gmail.processed_label)}"
    return [
        f"from:({' OR '.join(senders[i : i + SENDERS_PER_QUERY])}){suffix}"
        for i in range(0, len(senders), SENDERS_PER_QUERY)
    ]


def build_unlisted_query(config: AppConfig) -> str:
    """Recent mail in the main inbox from anyone (used only when smart reading is on). Mail sent
    by this account and mail from automatic senders is left out; the program still checks every
    message again after it is read."""
    query = (
        f"in:inbox category:primary newer_than:{config.gmail.lookback_days}d -from:me "
        f"-from:({AUTOMATIC_SENDERS})"
    )
    if config.gmail.apply_label:
        query += f" -label:{label_search_name(config.gmail.processed_label)}"
    return query


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
        if refresh_index:
            self._refresh_index(report)
        try:
            ids = self._new_ids()
        except AuthRequiredError:
            report.needs_sign_in = True
            return report
        except GmailError as exc:
            report.errors.append(f"could not list mail: {type(exc).__name__}")
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
        self.repo.log(
            "system", "mail_checked", None, examined=report.examined, requests=report.requests,
            ready=report.ready, review=report.needs_review, ignored=report.ignored,
            errors=len(report.errors),
        )  # fmt: skip
        return report

    # ------------------------------------------------------------------ steps
    def _refresh_index(self, report: IntakeReport) -> None:
        for scan in self.index.scan_all(self.config.all_roots()):
            if scan.status != "complete":
                report.warnings.append(f"Could not refresh the index for {scan.root}: {scan.error}")

    def _new_ids(self) -> list[str]:
        seen: dict[str, None] = {}
        for query in build_queries(self.config):
            room = self.config.gmail.max_messages - len(seen)
            if room <= 0:
                break
            for message_id in self.gateway.list_message_ids(query, room):
                seen.setdefault(message_id)
        room = self.config.gmail.max_messages - len(seen)
        if self.config.smart.enabled and room > 0:
            unlisted = build_unlisted_query(self.config)
            for message_id in self.gateway.list_message_ids(unlisted, room):
                seen.setdefault(message_id)
        return [m for m in seen if not self.repo.is_processed(m)]

    def _handle(self, message_id: str, report: IntakeReport) -> None:
        raw = self.gateway.get_message(message_id)
        report.examined += 1
        msg = parse_message(raw)

        if "SENT" in msg.label_ids:
            return self._ignore(msg, "sent by this account", report)
        dept = self.config.department_for_sender(msg.sender_email)
        if dept is None:
            if self.config.smart.enabled:
                return self._handle_unlisted(msg, report)
            return self._ignore(msg, "sender is not in any department", report)
        if self._is_followup_without_request(msg):
            return self._ignore(msg, "reply in a handled conversation with no new request", report)

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
        )
        if email_id is None:
            return None
        self.repo.log("system", "sender_checked", None, email_id=email_id,
                      verdict=auth.verdict.value, reason=auth.reason)  # fmt: skip
        ids = process_email_requests(
            self.repo, self.index, self.config, email_id, msg.subject, msg.body, today=self._today()
        )
        self._count(ids, report)
        self._label(msg.message_id, report)
        return None

    # ------------------------------------------------------------------ senders in no department
    def _handle_unlisted(self, msg: InboundEmail, report: IntakeReport) -> None:
        """An email from someone who is in no department. It becomes a request only if (1) lines
        of it are exactly the names of folders on the server, or (2) the language model finds
        names in it. Everything else is remembered as "not a request". Whatever is found waits
        for the person: there is no department, so nothing can be ready or replied to by itself."""
        if self._is_followup_without_request(msg):
            return self._ignore(msg, "reply in a handled conversation with no new request", report)
        found, _notes = exact_items(msg.subject, msg.body, self.index, None, found_only=True)
        names: list[str] = []
        if not found:
            reading = self._read_with_model(msg, report)
            if reading is None:
                return None  # the model could not be used now: look at it again next time
            if not reading.is_request:
                return self._ignore(msg, NOT_A_REQUEST, report)
            names = list(reading.folders)
        email_id = self._record_unlisted(msg)
        if email_id is None:
            return None
        if names:
            ids = process_named_requests(
                self.repo, self.index, self.config, email_id, names, today=self._today()
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
        self._count(ids, report)
        self._label(msg.message_id, report)
        return None

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
        """Remember we looked, so it is not fetched again. The mail itself is left untouched."""
        self.repo.add_email(
            gmail_message_id=msg.message_id,
            gmail_thread_id=msg.thread_id,
            sender_email=msg.sender_email,
            sender_name=msg.sender_name,
            subject=msg.subject,
            received_at=msg.received_at,
            auth_status="unknown",
            ignored_reason=reason,
        )
        report.ignored += 1

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

    def _count(self, request_ids: list[int], report: IntakeReport) -> None:
        for request_id in request_ids:
            row = self.repo.get_request(request_id)
            report.requests += 1
            report.request_ids.append(request_id)
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

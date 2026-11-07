"""Everything the screens do, with no Qt in it, so it can be tested without a window.

The UI calls these methods and shows what comes back. Rules that protect the user (nothing is
copied without "Confirm copy", an unverified sender needs an explicit acknowledgement, a path
must stay inside the configured folders, a "shared" reply only after a verified copy) are
enforced here and in the layers below, never only in a button's enabled state.

Methods that touch the network or a large folder (``check_now``, ``approve``, ``send_reply``,
``rescan_index``) are meant to be run off the GUI thread.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import Future
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from contentshare.app.telegram import TelegramError, TelegramNotifier, alert_text
from contentshare.app.users import UserError, UserStore
from contentshare.app.users import clean_name as clean_user_name
from contentshare.config.models import AppConfig, Department, DevSettings
from contentshare.config.store import data_dir, save_config
from contentshare.copy_engine import (
    PreflightError,
    Progress,
    build_plan,
    format_bytes,
    format_duration,
    preflight,
)
from contentshare.devtools.gateways import FakeGateway, NoSendGateway
from contentshare.devtools.logbuffer import attach as attach_log_buffer
from contentshare.devtools.logbuffer import detach as detach_log_buffer
from contentshare.devtools.trace import TRACE
from contentshare.gmail.auth import GmailAuth, required_scopes
from contentshare.gmail.errors import AuthRequiredError, GmailError
from contentshare.gmail.gateway import GmailGateway
from contentshare.gmail.intake import SCAN_MARKER, IntakeReport, IntakeService
from contentshare.gmail.message import parse_message
from contentshare.gmail.replies import ReplyDraft, ReplyError, ReplyResult, ReplyService
from contentshare.index.pcsearch import DEFAULT_SECONDS, search_pc
from contentshare.index.store import ContentIndex, IndexedItem, ScanReport, measure
from contentshare.jobs.copy_job import (
    CopyJobRunner,
    JobOutcome,
    options_from_config,
    paths_overlap,
    resolve_destination,
    resolve_source,
)
from contentshare.jobs.queue import CopyQueue
from contentshare.jobs.states import TERMINAL, InvalidTransitionError, RequestState
from contentshare.matching.scorer import Candidate, rank_candidates
from contentshare.matching.service import destination_for, process_named_requests, recheck_email
from contentshare.parsing.dates import date_phrases
from contentshare.parsing.request import RequestItem, parse_request
from contentshare.security.paths import PathSafetyError, resolve_within
from contentshare.smart.reader import SmartReader
from contentshare.smart.runtime import Installer, Runtime, SmartError, Status
from contentshare.smart.schema import Reading, reading_summary
from contentshare.storage.repository import Repository

log = logging.getLogger(__name__)

COPY_OK = "Copied and verified"  # the message of a successful ``copy_finished`` event

S = RequestState


GatewayFactory = Callable[[], GmailGateway]
MANUAL_RESULTS = 20
RECHECK_MINUTES = 1  # Refresh reads the storage folders again if they were read longer ago
UI_OPTIONS = (
    "appearance",
    "sidebar_collapsed",
    "compact_requests",
    "user_name",
    "setup_done",
)  # saved at once, see set_ui_option


class ActionError(Exception):
    """The action is refused. The message is written for the user and shown as is."""


@dataclass(frozen=True)
class Event:
    """Something changed in the background. ``kind`` is one of ``copy_progress``,
    ``copy_finished``, ``reply_sent``, ``reply_problem`` or ``changed``."""

    kind: str
    request_id: int | None = None
    message: str = ""


@dataclass(frozen=True)
class CandidateView:
    item_id: int | None
    path: str
    kind: str
    size: int
    file_count: int
    score: float
    notes: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CandidateView:
        return cls(
            item_id=data.get("item_id") or None,  # 0: content that is not in the index
            path=str(data.get("path", "")),
            kind=str(data.get("kind", "dir")),
            size=int(data.get("size", 0)),
            file_count=int(data.get("file_count", 0)),
            score=float(data.get("score", 0)),
            notes=tuple(str(n) for n in data.get("notes", [])),
        )

    @classmethod
    def from_candidate(cls, candidate: Candidate) -> CandidateView:
        return cls.from_dict(candidate.to_dict())


@dataclass(frozen=True)
class RequestView:
    """One request line, ready to display."""

    id: int
    email_id: int
    state: str
    sender_email: str
    sender_name: str
    department: str | None
    subject: str
    snippet: str
    received_at: str
    auth_status: str
    requested: str  # what was asked for, in words
    reason: str  # why it is READY / NEEDS_REVIEW
    warnings: tuple[str, ...]
    candidates: tuple[CandidateView, ...]
    source_path: str | None
    confidence: float | None
    file_count: int | None
    total_bytes: int | None
    destination_path: str | None
    auto_reply: bool
    failure_reason: str | None
    updated_at: str
    gmail_thread_id: str
    gmail_message_id: str
    copy_seconds: float | None = None  # how long the last copy of it took, if one finished
    smart: bool = False  # the names were read from the email by the language model
    approved_by: str = ""  # the Windows user who approved it ("" until someone does)

    @property
    def copy_time_text(self) -> str:
        return format_duration(self.copy_seconds) if self.copy_seconds is not None else ""

    @property
    def copy_speed_text(self) -> str:
        """Average speed of the whole job: the files were copied, flushed and read back."""
        if not self.copy_seconds or not self.total_bytes:
            return ""
        return f"{format_bytes(self.total_bytes / self.copy_seconds)}/s"

    @property
    def verified_sender(self) -> bool:
        return self.auth_status == "verified"

    @property
    def size_text(self) -> str:
        return format_bytes(self.total_bytes) if self.total_bytes is not None else ""


_NEEDS_THE_PERSON = frozenset({"NEEDS_REVIEW", "READY", "FAILED"})
_STILL_COPYING = frozenset({"APPROVED", "COPYING"})
_CAN_SKIP = frozenset({"NEEDS_REVIEW", "READY", "FAILED"})
_CAN_REJECT = frozenset({"NEEDS_REVIEW", "READY"})


@dataclass(frozen=True)
class EmailGroup:
    """Everything one email asked for. An email that lists several folders is ONE request with a
    line for each folder (each line is its own request in the database, so that each can be
    chosen, copied and checked on its own)."""

    email_id: int
    items: tuple[RequestView, ...]  # in the order they were asked for

    @property
    def lead(self) -> RequestView:
        return self.items[0]

    @property
    def id(self) -> int:
        """The first line's number: stands for the email on screen."""
        return self.items[0].id

    @property
    def many(self) -> bool:
        return len(self.items) > 1

    def in_states(self, states: Iterable[str]) -> list[RequestView]:
        wanted = set(states)
        return [v for v in self.items if v.state in wanted]

    @property
    def needs_the_person(self) -> bool:
        """Something here waits for a decision, an approval or a retry."""
        return any(v.state in _NEEDS_THE_PERSON for v in self.items)

    @property
    def still_copying(self) -> bool:
        return any(v.state in _STILL_COPYING for v in self.items)


@dataclass(frozen=True)
class GroupApproveResult:
    started: int  # lines whose copy was queued
    dry_run: bool = False
    files: int = 0
    total_bytes: int = 0
    problems: tuple[str, ...] = ()  # what stopped a line, or would stop it in a dry run
    notes: tuple[str, ...] = ()


def describe_item(data: dict[str, Any]) -> str:
    """The request in words: ``Sunrise Talk · 12 Oct 2026 · english · Day 2``."""
    parts = [str(data.get("program") or "").strip() or "(program not recognised)"]
    for entry in data.get("dates") or []:
        start, end = date.fromisoformat(entry[0]), date.fromisoformat(entry[1])
        text = f"{start.day} {start:%b %Y}"
        parts.append(text if start == end else f"{text} – {end.day} {end:%b %Y}")  # noqa: RUF001
    if data.get("language"):
        parts.append(str(data["language"]))
    if data.get("part"):
        parts.append(f"{data['part'][0]} {data['part'][1]}")
    return " · ".join(parts)


def _seconds(value: object) -> float | None:
    return max(0.0, float(value)) if isinstance(value, int | float) else None


def make_view(row: dict[str, Any]) -> RequestView:
    try:
        detail = json.loads(row.get("query_json") or "{}")
        if not isinstance(detail, dict):
            detail = {}
    except ValueError:
        detail = {}
    try:
        requested = describe_item(detail.get("item") or {})
    except (ValueError, IndexError, TypeError):
        requested = "(could not read the request)"
    candidates = tuple(
        CandidateView.from_dict(c) for c in detail.get("candidates", []) if isinstance(c, dict)
    )
    return RequestView(
        id=int(row["id"]),
        email_id=int(row["email_id"]),
        state=str(row["state"]),
        sender_email=str(row["sender_email"]),
        sender_name=str(row.get("sender_name") or ""),
        department=row.get("department"),
        subject=str(row.get("subject") or ""),
        snippet=str(row.get("snippet") or ""),
        received_at=str(row.get("received_at") or ""),
        auth_status=str(row["auth_status"]),
        requested=requested,
        reason=str(detail.get("reason") or ""),
        warnings=tuple(str(w) for w in detail.get("warnings", [])),
        candidates=candidates,
        source_path=row.get("source_path"),
        confidence=row.get("confidence"),
        file_count=row.get("file_count"),
        total_bytes=row.get("total_bytes"),
        destination_path=row.get("destination_path"),
        auto_reply=bool(row.get("auto_reply")),
        failure_reason=row.get("failure_reason"),
        updated_at=str(row.get("updated_at") or ""),
        gmail_thread_id=str(row.get("gmail_thread_id") or ""),
        gmail_message_id=str(row.get("gmail_message_id") or ""),
        copy_seconds=_seconds(row.get("copy_seconds")),
        approved_by=str(row.get("approved_by") or ""),
        smart=detail.get("source") == "smart",
    )


@dataclass(frozen=True)
class ApproveResult:
    started: bool  # the copy was queued
    dry_run: bool = False
    files: int = 0
    total_bytes: int = 0
    problems: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class ActiveCopy:
    request_id: int
    label: str
    department: str | None
    state: str
    progress: Progress | None
    paused: bool


@dataclass
class _Live:
    progress: Progress | None = None
    paused: bool = False


# Excel and others treat a cell starting with one of these as a formula. Subjects and names
# come from outside, so they are defused before being written to a CSV.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
HISTORY_COLUMNS = [
    "request", "received", "from", "department", "subject", "requested", "state", "source",
    "destination", "files", "bytes", "copy_seconds", "sender_check", "failure",
    "approved_by",
]  # fmt: skip


def csv_safe(value: object) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_START) else text


class Controller:
    def __init__(
        self,
        repo: Repository,
        index: ContentIndex,
        config: AppConfig,
        *,
        gateway_factory: GatewayFactory,
        auth: GmailAuth | None = None,
        config_path: Path | None = None,
        today: Callable[[], date] = date.today,
        telegram: TelegramNotifier | None = None,
        llm_root: Path | None = None,
    ) -> None:
        self.repo, self.index, self.config = repo, index, config
        self._llm_root = llm_root  # where the model and its program are kept (None: AppData)
        self._smart: Runtime | None = None
        self.telegram = telegram or TelegramNotifier()  # touches nothing until an alert is sent
        self._gateway_factory, self.auth = gateway_factory, auth
        self._config_path, self._today = config_path, today
        self.users = UserStore(repo)
        self._signed_in: str | None = None
        self.login_required = False  # the real app sets this: nothing is done before signing in
        self._live: dict[int, _Live] = {}
        self._lock = threading.Lock()
        self._check_lock = threading.Lock()
        self._reply_lock = threading.Lock()  # one automatic reply at a time (see _auto_reply)
        self._listeners: list[Callable[[Event], None]] = []
        self.queue = CopyQueue(
            CopyJobRunner(repo, config), concurrency=config.copy_options.concurrency
        )
        self._fake_gateway: FakeGateway | None = None
        self.apply_dev()

    # ------------------------------------------------------------------ developer mode
    def apply_dev(self) -> None:
        """Start or stop the developer tools to match the settings."""
        dev = self.config.dev
        TRACE.enabled = dev.enabled
        if dev.enabled:
            attach_log_buffer(dev.debug_log)
        else:
            detach_log_buffer()

    def set_dev_option(self, name: str, value: bool) -> None:
        """Change one developer switch at once (allowed while copies run: none of them touches
        a copy)."""
        if name not in DevSettings.model_fields:
            raise ValueError(f"{name} is not a developer option")
        dev = self.config.dev.model_copy(update={name: bool(value)})
        config = AppConfig.model_validate({**self.config.model_dump(), "dev": dev.model_dump()})
        save_config(config, self._config_path)
        self.config = config
        self.apply_dev()
        self._emit("changed")

    def _gateway(self) -> Any:
        """The Gmail gateway to use now: the real one, the test mailbox, or the real one with
        sending switched off (developer mode only)."""
        dev = self.config.dev
        if dev.enabled and dev.fake_gmail:
            if self._fake_gateway is None:
                first = next(
                    (d.sender_emails[0] for d in self.config.departments if d.sender_emails), None
                )
                self._fake_gateway = FakeGateway(first)
            return self._fake_gateway
        gateway = self._gateway_factory()
        return NoSendGateway(gateway) if dev.enabled and dev.no_replies else gateway

    def dev_gateway(self) -> Any:
        return self._gateway()

    def forget_all_skipped(self) -> int:
        """Developer mode: every mail that was looked at and left alone is read again at the
        next check (requests already made are not touched)."""
        count = 0
        for row in self.repo.ignored_emails(limit=100000):
            count += int(self.repo.forget_ignored_email(int(row["id"])))
        if count:
            self.repo.set_scan_value(SCAN_MARKER, None)  # the next check reads the first days again
            self._fake_gateway = None  # the test mailbox starts again too
            self._emit("changed")
        return count

    # ------------------------------------------------------------------ events
    def subscribe(self, listener: Callable[[Event], None]) -> None:
        self._listeners.append(listener)

    def _emit(self, kind: str, request_id: int | None = None, message: str = "") -> None:
        event = Event(kind, request_id, message)
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                log.exception("listener failed for %s", kind)

    # ------------------------------------------------------------------ reading
    @property
    def dry_run(self) -> bool:
        return self.config.dry_run

    def views(self, states: Iterable[RequestState] | None = None) -> list[RequestView]:
        return [make_view(r) for r in self.repo.list_requests(states)]

    def groups(self, states: Iterable[RequestState] | None = None) -> list[EmailGroup]:
        """The requests, one entry per email. An email is listed when any of its lines is in
        ``states``, and then with all of its lines."""
        wanted = None if states is None else {str(s) for s in states}
        by_email: dict[int, list[RequestView]] = {}
        for view in self.views():
            by_email.setdefault(view.email_id, []).append(view)
        return [
            EmailGroup(email_id, tuple(sorted(items, key=lambda v: v.id)))
            for email_id, items in by_email.items()
            if wanted is None or any(v.state in wanted for v in items)
        ]

    def attention_count(self) -> int:
        """Emails with something waiting for the person (the number beside Requests)."""
        return sum(1 for g in self.groups() if g.needs_the_person)

    def group(self, email_id: int) -> EmailGroup:
        items = tuple(v for v in self.views() if v.email_id == email_id)
        if not items:
            raise ActionError("This request no longer exists")
        return EmailGroup(email_id, tuple(sorted(items, key=lambda v: v.id)))

    def view(self, request_id: int) -> RequestView:
        row = self.repo.get_request(request_id)
        if row is None:
            raise ActionError(f"Request {request_id} no longer exists")
        return make_view(row)

    def last_check(self) -> datetime | None:
        stamp = self.repo.last_event_time("mail_checked")
        return datetime.fromisoformat(stamp) if stamp else None

    def audit(self, request_id: int) -> list[dict[str, Any]]:
        return self.repo.audit_entries(request_id)

    def replies_for(self, request_id: int) -> list[dict[str, Any]]:
        return self.repo.list_replies(request_id)

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.repo.list_requests():
            counts[row["state"]] = counts.get(row["state"], 0) + 1
        return counts

    # ------------------------------------------------------------------ review actions
    def _row(self, request_id: int) -> dict[str, Any]:
        row = self.repo.get_request(request_id)
        if row is None:
            raise ActionError(f"Request {request_id} no longer exists")
        return row

    def _department(self, name: str | None) -> Any:
        dept = self.config.department_for(name)
        if dept is None:
            raise ActionError(f"“{name}” is not a department in the settings; add it first")
        return dept

    def _transition(self, request_id: int, target: RequestState, **kwargs: Any) -> None:
        self._require_sign_in()
        try:
            self.repo.transition(request_id, target, actor=self.user_name, **kwargs)
        except InvalidTransitionError as exc:
            raise ActionError(str(exc)) from exc
        except PermissionError as exc:
            raise ActionError(str(exc)) from exc

    def add_folder(self, email_id: int, text: str) -> int:
        """Add a line to this email for a folder the person names (for example one the email
        mentioned that was not searched). It starts as Needs review; the content is chosen
        with the usual search. Always possible, also for an email that was already copied or
        answered: the new line is copied and checked on its own, and a folder that is verified
        after the reply was sent gets a short follow-up reply of its own. Returns the new
        request's number."""
        name = clean_user_name(text, limit=200)
        if not name:
            raise ActionError("Type the name of the folder")
        email = self.repo.get_email(email_id)
        if email is None:
            raise ActionError("This request no longer exists")
        if email.get("department") and self._department_or_none(email.get("department")) is None:
            raise ActionError("This sender's department is not in the settings; add it first")
        number = max((r["line_no"] for r in self.repo.requests_of_email(email_id)), default=0) + 1
        item = RequestItem(program=name, raw=name)
        request_id = self.repo.add_request(
            email_id, number, {"item": item.to_dict()}, auto_reply=self.config.auto_reply_default
        )
        detail = {
            "item": item.to_dict(),
            "reason": "Added by you: choose the folder to copy",
            "candidates": [],
            "warnings": [],
            "added": True,
        }
        self._transition(
            request_id, S.NEEDS_REVIEW, reason="folder added by the user",
            query_json=json.dumps(detail),
        )  # fmt: skip
        self._emit("changed", request_id)
        return request_id

    def refresh_email(self, email_id: int) -> str:
        """Look the folders of this email that still wait for content up again in the index (the
        storage folders are read again first unless that was done in the last minute). Returns a
        sentence for the person. Folders somebody chose content for are left alone."""
        self._require_sign_in()
        if self.repo.get_email(email_id) is None:
            raise ActionError("This request no longer exists")
        result = recheck_email(
            self.repo,
            self.index,
            self.config,
            email_id,
            actor=self.user_name,
            today=self._today(),
            minutes=RECHECK_MINUTES,
        )
        if result.found or result.updated or result.scanned:
            self._emit("changed")
        return result.message

    def ask_model_about_email(self, email_id: int) -> str:
        """Read this email again with the local model (the text is fetched from Gmail) and add
        a line for each folder it names that is not listed yet. Works whatever the master switch
        says: the person asked for it. Returns a sentence for the person."""
        self._require_sign_in()
        email = self.repo.get_email(email_id)
        if email is None:
            raise ActionError("This request no longer exists")
        try:
            msg = parse_message(self._gateway().get_message(str(email["gmail_message_id"])))
        except Exception as exc:  # not connected, offline, or the email was deleted
            raise ActionError(f"The email could not be read from Gmail again: {exc}") from exc
        try:
            reading = SmartReader(self.smart_runtime(), self.repo, self.config.smart).read(
                msg.subject, msg.body
            )
        except SmartError as exc:
            raise ActionError(str(exc)) from exc
        lines = self.repo.requests_of_email(email_id)
        for row in lines:
            self.repo.log("model", "model_read", row["id"], reason=reading_summary(reading))
        if not reading.is_request:
            return "The model says this email is not a content request."
        listed = set()
        for row in lines:
            try:
                listed.add(
                    str(json.loads(row.get("query_json") or "{}")["item"]["program"]).casefold()
                )
            except (ValueError, KeyError, TypeError):
                continue
        names = [n for n in reading.folders if n.casefold() not in listed]
        if not names:
            return (
                f"The model found no folder that is not listed already ({reading.seconds:.1f} s)."
            )
        ids = process_named_requests(
            self.repo, self.index, self.config, email_id, names, today=self._today(),
            first_line=max((r["line_no"] for r in lines), default=0) + 1,
        )  # fmt: skip
        self._emit("changed")
        return f"The model found {len(ids)} new folder(s) in {reading.seconds:.1f} s; look at them."

    def discard_added_folder(self, request_id: int) -> None:
        """The person added a folder and then chose nothing: take the empty line away again."""
        row = self._row(request_id)
        try:
            added = bool(json.loads(row.get("query_json") or "{}").get("added"))
        except ValueError:
            added = False
        if added and row["state"] == S.NEEDS_REVIEW:
            self.repo.delete_unused_request(request_id, actor=self.user_name)
            self._emit("changed")

    def search(self, request_id: int, text: str) -> list[CandidateView]:
        """Manual search. The typed text is read like a request of its own (so typing a date or
        a language narrows the results), and the original email's date and language are not
        applied: the user may be looking for a different program altogether."""
        text = text.strip()
        if not text:
            return []
        row = self._row(request_id)  # the request must still exist
        item = self._typed_item(text)
        roots = self.config.roots_for(self._department_or_none(row["department"]))
        hits = self.index.search(text, date_phrases=date_phrases(list(item.dates)), roots=roots)
        ranked = rank_candidates(
            item, hits, self.config.parsing, today=self._today(), limit=MANUAL_RESULTS
        )
        return [CandidateView.from_candidate(c) for c in ranked]

    def _typed_item(self, text: str) -> RequestItem:
        parsed = parse_request(text, "", self.config.parsing, today=self._today())
        item = parsed.items[0] if parsed.items else RequestItem(program=text)
        return replace(item, program=text) if not item.program.strip() else item

    def _department_or_none(self, name: str | None) -> Department | None:
        return self.config.department_for(name)

    def pc_search_allowed(self, request_id: int) -> bool:
        """True if the request's department may fall back to searching this whole PC."""
        dept = self._department_or_none(self._row(request_id)["department"])
        return bool(dept and dept.search_whole_pc)

    def search_pc(
        self,
        request_id: int,
        text: str = "",
        *,
        should_stop: Callable[[], bool] | None = None,
        progress: Callable[[int, str], None] | None = None,
        seconds: float = DEFAULT_SECONDS,
    ) -> tuple[list[CandidateView], bool]:
        """The wider search: this PC's own drives, for a department that allows it. ``text``
        empty means the program the email asked for. Returns the matches (best first, never
        selected for the person) and whether the whole PC was covered before it stopped.
        Slow: call it from a background thread."""
        row = self._row(request_id)
        dept = self._department(row["department"])
        if not dept.search_whole_pc:
            raise ActionError(
                f"Searching the whole PC is switched off for {dept.name} (Settings → Departments)."
            )
        text = text.strip()
        if text:
            item = self._typed_item(text)
        else:
            try:
                detail = json.loads(row.get("query_json") or "{}")
                item = RequestItem.from_dict(detail.get("item") or {})
            except (ValueError, IndexError, TypeError):
                item = RequestItem(program="")
        if not item.program.strip():
            return [], True
        found = search_pc(
            item.program,
            skip=[d.destination_root for d in self.config.departments],
            should_stop=should_stop,
            progress=progress,
            seconds=seconds,
        )
        ranked = rank_candidates(
            item, found.items, self.config.parsing, today=self._today(), limit=MANUAL_RESULTS
        )
        return [CandidateView.from_candidate(c) for c in ranked], found.complete

    def choose(self, request_id: int, item_id: int) -> None:
        """The user picked this indexed content for the request (NEEDS_REVIEW or READY).
        The path is re-resolved inside the source folders and measured on disk."""
        row = self._row(request_id)
        if row["state"] not in (S.NEEDS_REVIEW, S.READY):
            raise ActionError("This request can no longer be changed")
        indexed = self.index.get(item_id)
        if indexed is None:
            raise ActionError("That content is not in the index any more; refresh the index")
        self._use_content(row, indexed)

    def choose_path(self, request_id: int, path: str) -> None:
        """The user browsed to a folder or file on the storage server instead of searching.
        It must be inside one of the source folders (and not a source folder itself). It does
        not have to be in the index, so content added since the last index refresh works."""
        row = self._row(request_id)
        if row["state"] not in (S.NEEDS_REVIEW, S.READY):
            raise ActionError("This request can no longer be changed")
        dept = self._department(row["department"])
        found = None
        for root in self.config.roots_for(dept):
            try:
                source = resolve_within(root, path.strip())
                found = (root, source, source.relative_to(Path(os.path.realpath(root))))
                break
            except (PathSafetyError, ValueError):
                continue
        if found is None:
            self._use_picked_anywhere(row, dept, path.strip())
            return
        root, source, relative = found
        if not relative.parts:
            raise ActionError(
                "Choose a folder or file inside a storage folder, not the storage folder itself"
            )
        rel = relative.as_posix()
        indexed = self.index.find_path(root, rel)
        if indexed is None:  # new since the last scan: describe it from the disk
            try:
                measured = measure(source)
            except OSError as exc:
                raise ActionError(f"Cannot read that content: {exc}") from exc
            if measured is None:
                raise ActionError("That content is not on the storage server")
            indexed = IndexedItem(
                0, root, rel, source.name, "dir" if source.is_dir() else "file",
                measured[0], measured[1], 0, rel.count("/"),
            )  # fmt: skip
        self._use_content(row, indexed)

    def _use_picked_anywhere(self, row: dict[str, Any], dept: Any, path: str) -> None:
        """A folder or file outside the storage folders, for a department allowed to use one."""
        if not dept.may_pick_anywhere:
            raise ActionError(
                f"That is outside the storage folders for {dept.name}. Choose something inside "
                "one of them, or turn on “Allow choosing from anywhere” for "
                f"{dept.name} in Settings → Departments."
            )
        try:
            source = resolve_source(self.config, dept, path)
        except PathSafetyError as exc:
            raise ActionError(f"That cannot be used: {exc}") from exc
        try:
            measured = measure(source)
        except OSError as exc:
            raise ActionError(f"Cannot read that content: {exc}") from exc
        if measured is None:
            raise ActionError("That content could not be found")
        indexed = IndexedItem(
            0, str(source.parent), source.name, source.name,
            "dir" if source.is_dir() else "file", measured[0], measured[1], 0, 0,
        )  # fmt: skip
        self._use_content(row, indexed, source)

    def _use_content(
        self, row: dict[str, Any], indexed: IndexedItem, picked: Path | None = None
    ) -> None:
        """Point the request at this content (checked again on disk) and make it READY."""
        request_id = int(row["id"])
        dept = self._department(row["department"])
        try:
            source = picked or resolve_source(self.config, dept, indexed.full_path)
        except PathSafetyError as exc:
            raise ActionError(f"That content is outside the source folders: {exc}") from exc
        if paths_overlap(source, Path(dept.destination_root)):
            raise ActionError(
                "That folder is inside, or contains, this department's destination folder"
            )
        try:
            measured = measure(source)
        except OSError as exc:
            raise ActionError(f"Cannot read that content: {exc}") from exc
        if measured is None:
            raise ActionError("That content is not on the storage server any more")
        size, count = measured

        detail = json.loads(row.get("query_json") or "{}")
        try:
            request_item = RequestItem.from_dict(detail.get("item") or {})
        except (ValueError, IndexError, TypeError):
            request_item = RequestItem(program="")
        scored = rank_candidates(
            request_item, [indexed], self.config.parsing, today=self._today(), limit=1
        )
        try:
            destination = destination_for(
                dept, Candidate(indexed, 0.0, 0.0, 0.0, (), frozenset()), request_item
            )
        except PathSafetyError as exc:
            raise ActionError(f"Destination not allowed: {exc}") from exc

        fields: dict[str, Any] = {
            "source_path": str(source),
            "confidence": scored[0].score if scored else None,
            "file_count": count,
            "total_bytes": size,
            "destination_path": str(destination),
        }
        if row["state"] == S.NEEDS_REVIEW:
            self._transition(request_id, S.READY, reason="content chosen by the user", **fields)
        else:
            self.repo.update_request(
                request_id, actor=self.user_name, reason="content changed by the user", **fields
            )
        self._emit("changed", request_id)

    def set_destination(self, request_id: int, path: str) -> None:
        row = self._row(request_id)
        if row["state"] not in (S.NEEDS_REVIEW, S.READY):
            raise ActionError("The destination can only be changed before approval")
        dept = self._department(row["department"])
        try:
            resolved = resolve_destination(dept, path.strip())
        except PathSafetyError as exc:
            raise ActionError(
                f"The destination must be inside {dept.destination_root}: {exc}"
            ) from exc
        self.repo.update_request(
            request_id, actor=self.user_name, reason="destination edited",
            destination_path=str(resolved),
        )  # fmt: skip
        self._emit("changed", request_id)

    def set_auto_reply(self, request_id: int, enabled: bool) -> None:
        try:
            self.repo.update_request(
                request_id, actor=self.user_name, reason="auto-reply toggled",
                auto_reply=int(enabled),
            )  # fmt: skip
        except InvalidTransitionError as exc:
            raise ActionError("This request is finished and cannot be changed") from exc

    def skip(self, request_id: int) -> None:
        self._transition(request_id, S.SKIPPED, reason="skipped by the user")
        self._learn_dismissed(int(self._row(request_id)["email_id"]))
        self._emit("changed", request_id)

    def restore(self, request_id: int) -> None:
        """Put a skipped request back into the Requests list (it was skipped by mistake). It is
        Ready again when it still has its content and destination, else it waits for review."""
        row = self._row(request_id)
        if row["state"] != S.SKIPPED:
            raise ActionError("Only a skipped request can be put back")
        ready = bool(row["source_path"] and row["destination_path"])
        self._transition(
            request_id,
            S.READY if ready else S.NEEDS_REVIEW,
            reason="put back by the user",
        )
        self._emit("changed", request_id)

    def ignored_mail(self) -> list[dict[str, Any]]:
        """Mail the program looked at and left alone, with why (for the History)."""
        return self.repo.ignored_emails()

    def mailbox(self) -> list[dict[str, Any]]:
        """Every mail looked at (requests and skipped), newest first, for the Mailbox view."""
        return self.repo.mailbox()

    def _mail_service(self) -> IntakeService:
        """An intake service for one mail the person picked. Not while a check is running."""
        if self._check_lock.locked():
            raise ActionError("A check is running; try again when it has finished")
        try:
            gateway = self._gateway()
        except AuthRequiredError as exc:
            raise ActionError("Connect Gmail first (Settings → Gmail)") from exc
        return IntakeService(
            gateway, self.repo, self.index, self.config, today=self._today, reader=self._reader()
        )

    def move_to_requests(self, email_ids: list[int]) -> list[int]:
        """The person says these skipped mails are requests. Each becomes a request (the sender
        is not asked, the model is not asked); with no names found it is one empty line that
        folders are added to. Returns the new request numbers."""
        service = self._mail_service()
        made: list[int] = []
        try:
            for email_id in email_ids:
                row = self.repo.get_email(email_id)
                if row is None or row["ignored_reason"] is None:
                    continue
                ids = service.move_to_requests(str(row["gmail_message_id"]))
                if ids and self.config.smart.learn:
                    self._remember(email_id, True, [])
                made.extend(ids)
        except GmailError as exc:
            raise ActionError(f"Gmail could not be read: {exc}") from exc
        if made:
            self._emit("changed", made[0])
        return made

    def read_mail_again(self, email_ids: list[int]) -> int:
        """Read these skipped mails again now (the model is asked once more). Returns how many
        became requests."""
        service = self._mail_service()
        count = 0
        try:
            for email_id in email_ids:
                row = self.repo.get_email(email_id)
                if row is None or row["ignored_reason"] is None:
                    continue
                count += service.read_again(str(row["gmail_message_id"])).requests
        except GmailError as exc:
            raise ActionError(f"Gmail could not be read: {exc}") from exc
        self._emit("changed", None)
        return count

    def always_skip_sender(self, sender: str, skip: bool = True) -> None:
        """Mail from this address is skipped without being read (``skip=False``: not any more)."""
        self.repo.set_sender_rule(sender, "skip" if skip else None)

    def always_accept_sender(self, sender: str, accept: bool = True) -> None:
        """Mail from this address is always taken as a request, without judging it."""
        self.repo.set_sender_rule(sender, "request" if accept else None)

    def sender_rule(self, sender: str) -> str | None:
        return self.repo.sender_rule(sender)

    def reject(self, request_id: int, reason: str = "") -> None:
        self._transition(request_id, S.REJECTED, reason=reason.strip()[:200] or "rejected")
        self._learn_dismissed(int(self._row(request_id)["email_id"]))
        self._emit("changed", request_id)

    def set_auto_reply_group(self, email_id: int, enabled: bool) -> None:
        """The "send the reply by itself" choice, for every line of the email that is not done."""
        for view in self.group(email_id).items:
            if view.state not in TERMINAL:
                self.set_auto_reply(view.id, enabled)

    def skip_group(self, email_id: int) -> list[int]:
        """Skip every line of the email that still waits for the person. Returns their numbers."""
        ids = [v.id for v in self.group(email_id).in_states(_CAN_SKIP)]
        if not ids:
            raise ActionError("Nothing in this request can be skipped any more")
        for request_id in ids:
            self._transition(request_id, S.SKIPPED, reason="skipped by the user")
        self._learn_dismissed(email_id)
        self._emit("changed", ids[0])
        return ids

    def reject_group(self, email_id: int, reason: str = "") -> list[int]:
        """Reject every line of the email that has not been approved. Returns their numbers."""
        ids = [v.id for v in self.group(email_id).in_states(_CAN_REJECT)]
        if not ids:
            raise ActionError("Nothing in this request can be rejected any more")
        for request_id in ids:
            self._transition(request_id, S.REJECTED, reason=reason.strip()[:200] or "rejected")
        self._learn_dismissed(email_id)
        self._emit("changed", ids[0])
        return ids

    # ------------------------------------------------------------------ approving and copying
    def approve(
        self,
        request_id: int,
        *,
        confirmed: bool,
        acknowledge_unverified: bool = False,
        automatic: bool = False,
    ) -> ApproveResult:
        """Approve a READY request and start copying. Re-checks the source on disk first.
        In dry-run mode everything is checked and reported but nothing changes. ``automatic`` is
        only for the setting “copy and reply by itself”: nobody is signed in to approve then."""
        if not automatic:
            self._require_sign_in()
        if not confirmed:
            raise ActionError("Tick “Confirm copy” first: nothing is copied without it")
        row = self._row(request_id)
        if row["state"] != S.READY:
            raise ActionError(f"Only a Ready request can be approved (this one is {row['state']})")
        if row["auth_status"] != "verified" and not acknowledge_unverified:
            raise ActionError(
                "The sender could not be verified. Check the email in Gmail, then tick "
                "“I checked this sender” to continue."
            )
        dept = self._department(row["department"])
        if not row["source_path"] or not row["destination_path"]:
            raise ActionError("Choose the content and a destination first")
        try:
            src = resolve_source(self.config, dept, row["source_path"])
            dst = resolve_destination(dept, row["destination_path"])
        except PathSafetyError as exc:
            raise ActionError(f"Path not allowed: {exc}") from exc
        if paths_overlap(src, dst):
            raise ActionError("The source and the destination overlap")

        note = self._recheck_on_disk(request_id, row, src)
        if self.config.dry_run:
            return self._dry_run(request_id, src, dst, note)

        if automatic:
            try:
                self.repo.transition(
                    request_id, S.APPROVED, actor="automatic",
                    reason="approved automatically: Ready and matched at 100%", auto_reply=1,
                )  # fmt: skip
            except (InvalidTransitionError, PermissionError) as exc:
                raise ActionError(str(exc)) from exc
        else:
            self._transition(
                request_id, S.APPROVED, reason="approved by the user", auto_reply=row["auto_reply"]
            )
        self._submit(request_id)
        self._learn_accepted(int(row["email_id"]))
        self._emit("changed", request_id)
        return ApproveResult(started=True, note=note)

    def auto_copy_certain(self) -> list[int]:
        """With the setting on: copy (and so reply to) every request that is Ready, from a
        verified sender and matched at exactly 100%. A request that cannot be copied stays Ready
        for a person. Returns the requests that were started."""
        if not self.config.auto_copy_certain or self.config.dry_run:
            return []
        started: list[int] = []
        for row in self.repo.list_requests([S.READY]):
            if row["auth_status"] != "verified" or float(row.get("confidence") or 0) < 100:
                continue
            try:
                self.approve(int(row["id"]), confirmed=True, automatic=True)
            except ActionError as exc:
                self.repo.log("automatic", "auto_copy_skipped", int(row["id"]), reason=str(exc))
                continue
            started.append(int(row["id"]))
        return started

    def approve_group(
        self, email_id: int, *, confirmed: bool, acknowledge_unverified: bool = False
    ) -> GroupApproveResult:
        """Approve every Ready line of one email. A line that cannot be approved (its folder is
        gone, a path is not allowed) is reported and the others go on."""
        if not confirmed:
            raise ActionError("Tick “Confirm copy” first: nothing is copied without it")
        ready = self.group(email_id).in_states({"READY"})
        if not ready:
            raise ActionError("Nothing in this request is Ready to copy")
        started, files, size = 0, 0, 0
        problems: list[str] = []
        notes: list[str] = []
        dry = False
        for view in ready:
            try:
                result = self.approve(
                    view.id, confirmed=True, acknowledge_unverified=acknowledge_unverified
                )
            except ActionError as exc:
                if len(ready) == 1:
                    raise
                problems.append(f"{view.requested}: {exc}")
                continue
            started += int(result.started)
            dry = dry or result.dry_run
            files += result.files
            size += result.total_bytes
            problems += [f"{view.requested}: {p}" for p in result.problems]
            if result.note:
                notes.append(f"{view.requested}: {result.note}" if len(ready) > 1 else result.note)
        return GroupApproveResult(started, dry, files, size, tuple(problems), tuple(notes))

    def _recheck_on_disk(self, request_id: int, row: dict[str, Any], src: Path) -> str:
        """The index may be hours old. Look at the real folder before copying."""
        try:
            measured = measure(src)
        except OSError as exc:
            raise ActionError(f"Cannot read the content on the server: {exc}") from exc
        if measured is None:
            raise ActionError(
                "The content is no longer on the storage server at the matched location. "
                "Refresh the index and choose it again."
            )
        size, count = measured
        if (size, count) == (row["total_bytes"], row["file_count"]):
            return ""
        self.repo.update_request(
            request_id, actor="system", reason="size re-measured before copy",
            total_bytes=size, file_count=count,
        )  # fmt: skip
        return (
            f"The content changed since it was indexed "
            f"({count} files, {format_bytes(size)} now); that is what will be copied."
        )

    def _dry_run(self, request_id: int, src: Path, dst: Path, note: str) -> ApproveResult:
        plan = build_plan(src, dst)
        problems: tuple[str, ...] = ()
        try:
            preflight(plan, options_from_config(self.config), create_dirs=False)
        except PreflightError as exc:
            problems = tuple(exc.problems)
        self.repo.log(
            self.user_name, "dry_run", request_id, files=len(plan.files), problems=len(problems)
        )
        return ApproveResult(
            started=False, dry_run=True, files=len(plan.files), total_bytes=plan.total_bytes,
            problems=problems, note=note,
        )  # fmt: skip

    def retry(self, request_id: int) -> None:
        """Run a FAILED request again (verified files are skipped)."""
        row = self._row(request_id)
        if row["state"] != S.FAILED:
            raise ActionError("Only a failed copy can be retried")
        if self.config.dry_run:
            raise ActionError("Dry-run mode is on: turn it off to copy for real")
        self._transition(request_id, S.APPROVED, reason="retry by the user", failure_reason=None)
        self._submit(request_id)
        self._emit("changed", request_id)

    def resume_approved(self) -> list[int]:
        """After a restart: requests the user approved earlier whose copy did not finish go on
        where they stopped. Nothing new is approved."""
        started: list[int] = []
        if self.config.dry_run:
            return started
        for request_id in self.repo.recover_interrupted():
            self.repo.log("system", "resume_after_restart", request_id)
        for row in self.repo.list_requests([S.APPROVED]):
            if self.queue.get(int(row["id"])) is None:
                self._submit(int(row["id"]))
                started.append(int(row["id"]))
        return started

    def _submit(self, request_id: int) -> None:
        with self._lock:
            self._live[request_id] = _Live()

        def progress(p: Progress) -> None:
            with self._lock:
                live = self._live.setdefault(request_id, _Live())
                live.progress = p
            self._emit("copy_progress", request_id)

        handle = self.queue.submit(request_id, progress=progress)
        handle.future.add_done_callback(lambda f: self._copy_done(request_id, f))

    def _copy_done(self, request_id: int, future: Future[JobOutcome]) -> None:
        with self._lock:
            self._live.pop(request_id, None)
        try:
            outcome = future.result()
        except Exception as exc:  # the runner reports its own failures; this is a bug
            log.exception("copy job crashed for request %s", request_id)
            message = f"Copy stopped unexpectedly ({type(exc).__name__})"
            self._emit("copy_finished", request_id, message)
            return
        if outcome.state == S.VERIFIED:
            self._emit("copy_finished", request_id, COPY_OK)
        else:
            self._emit("copy_finished", request_id, outcome.error or "Copy did not finish")
        # Also after a failure: the other folders of the same email may all be done by now, and
        # the one reply for the email waits for the last of them.
        self._auto_reply(request_id)

    def _auto_reply(self, request_id: int) -> None:
        try:
            # Two folders of one email can finish together; the one reply for the email is
            # worked out and sent by one of them at a time, so it is never sent twice.
            with self._reply_lock:
                result = self._replies().maybe_auto_reply(request_id)
        except AuthRequiredError:
            self._emit("reply_problem", request_id, "Gmail needs to be connected again")
        except (GmailError, ReplyError) as exc:
            self._emit("reply_problem", request_id, str(exc))
        else:
            if result is not None:
                self._after_reply(request_id, result)

    def pause(self, request_id: int) -> None:
        self.queue.pause(request_id)
        with self._lock:
            if request_id in self._live:
                self._live[request_id].paused = True
        self._emit("copy_progress", request_id)

    def resume(self, request_id: int) -> None:
        self.queue.resume(request_id)
        with self._lock:
            if request_id in self._live:
                self._live[request_id].paused = False
        self._emit("copy_progress", request_id)

    def cancel(self, request_id: int) -> None:
        self.queue.cancel(request_id)

    def active_copies(self) -> list[ActiveCopy]:
        out = []
        for request_id in self.queue.active():
            row = self.repo.get_request(request_id)
            if row is None:
                continue
            with self._lock:
                live = self._live.get(request_id, _Live())
            out.append(
                ActiveCopy(
                    request_id=request_id,
                    label=Path(row["source_path"] or "").name or f"Request {request_id}",
                    department=row["department"],
                    state=row["state"],
                    progress=live.progress,
                    paused=live.paused,
                )
            )
        return out

    def busy(self) -> bool:
        return bool(self.queue.active())

    # ------------------------------------------------------------------ gmail
    def _replies(self) -> ReplyService:
        return ReplyService(self.repo, self._gateway(), self.config, today=self._today)

    def check_now(
        self, *, refresh_index: bool = False, should_stop: Callable[[], bool] | None = None
    ) -> IntakeReport:
        # A manual click and the daily schedule must not read the same mailbox at once.
        if not self._check_lock.acquire(blocking=False):
            return IntakeReport(warnings=["A check is already running."], already_running=True)
        try:
            try:
                gateway = self._gateway()
            except AuthRequiredError:
                return IntakeReport(needs_sign_in=True)
            service = IntakeService(
                gateway, self.repo, self.index, self.config, today=self._today,
                reader=self._reader(),
            )  # fmt: skip
            report = service.check_now(refresh_index=refresh_index, should_stop=should_stop)
        finally:
            self._check_lock.release()
        started = self.auto_copy_certain()
        if started:
            report.warnings.append(f"{len(started)} request(s) were copied automatically.")
        self._emit("changed")
        self._alert_telegram(report)
        return report

    def _alert_telegram(self, report: IntakeReport) -> None:
        """Tell the Telegram group (if set up) that new requests wait for approval. A failure is
        only reported: the requests are on screen either way."""
        settings = self.config.telegram
        text = alert_text(report)
        if not settings.enabled or not settings.chat_id or text is None:
            return
        try:
            self.telegram.send(settings.chat_id, text)
        except TelegramError as exc:
            log.warning("telegram alert not sent: %s", exc)
            self._emit("telegram_problem", None, str(exc))
        except Exception as exc:  # the credential store can fail in ways of its own
            log.warning("telegram alert not sent: %s", type(exc).__name__)
            self._emit("telegram_problem", None, "The Telegram alert could not be sent.")

    def index_is_stale(self, *, max_age_hours: int = 20) -> bool:
        """True if any storage folder was never scanned or not scanned recently."""
        if not self.config.all_roots():
            return False
        now = datetime.now(UTC)
        for root in self.config.all_roots():
            scan = self.index.last_scan(root)
            stamp = scan.get("finished_at") if scan else None
            try:
                finished = datetime.fromisoformat(stamp) if stamp else None
            except ValueError:
                finished = None
            if finished is None:
                return True
            if finished.tzinfo is None:
                finished = finished.replace(tzinfo=UTC)
            if now - finished > timedelta(hours=max_age_hours):
                return True
        return False

    def scheduled_check(self, should_stop: Callable[[], bool] | None = None) -> IntakeReport:
        """The daily check: refresh a stale index, then read new mail. Creates request cards
        only; approving, copying and replying stay manual."""
        return self.check_now(refresh_index=self.index_is_stale(), should_stop=should_stop)

    def reply_draft(self, request_id: int) -> ReplyDraft:
        """The editable “content shared” message. Raises ``ActionError`` unless verified."""
        try:
            return self._replies().draft_shared(request_id)
        except ReplyError as exc:
            raise ActionError(str(exc)) from exc

    def reply_draft_for_email(self, email_id: int) -> ReplyDraft:
        """One “content shared” message for everything of this email that was verified."""
        try:
            return self._replies().draft_shared_for_email(email_id)
        except ReplyError as exc:
            raise ActionError(str(exc)) from exc

    def rejection_draft(self, request_id: int, text: str | None = None) -> ReplyDraft:
        try:
            return self._replies().draft_rejection(request_id, text)
        except ReplyError as exc:
            raise ActionError(str(exc)) from exc

    def send_reply(self, draft: ReplyDraft) -> ReplyResult:
        self._require_sign_in()
        if self.config.dry_run:
            raise ActionError("Dry-run mode is on: no email is sent. Turn it off to reply.")
        try:
            result = self._replies().send(draft, actor=self.user_name)
        except ReplyError as exc:
            raise ActionError(str(exc)) from exc
        self._after_reply(draft.request_id, result)
        return result

    def _after_reply(self, request_id: int, result: ReplyResult) -> None:
        if result.status == "SENT":
            self._emit("reply_sent", request_id, "Reply sent")
        else:
            self._emit("reply_problem", request_id, result.error or "The reply was not sent")
        self._emit("changed", request_id)

    def recover_replies(self) -> list[ReplyResult]:
        """Settle replies a crash left half-way (looks in Gmail; sends nothing new)."""
        if not self.repo.list_replies(status="SENDING"):
            return []
        try:
            return self._replies().recover_unfinished()
        except GmailError:
            return []

    def gmail_connected(self) -> bool:
        return bool(self.auth and self.auth.is_connected())

    def connect_gmail(self) -> None:
        if self.auth is None:
            raise ActionError("Gmail sign-in is not available")
        secret = self.config.gmail.client_secret_path
        if not secret or not Path(secret).is_file():
            raise ActionError(
                "Choose the client_secret.json file you downloaded from Google Cloud "
                "(Settings → Gmail) first."
            )
        try:
            self.auth.connect(secret, required_scopes(self.config.gmail))
        except GmailError as exc:
            raise ActionError(str(exc)) from exc

    def disconnect_gmail(self) -> str:
        if self.auth is None:
            raise ActionError("Gmail sign-in is not available")
        outcome = self.auth.disconnect()
        return (
            "Access revoked at Google and the saved sign-in was deleted."
            if outcome.revoked
            else f"The saved sign-in was deleted, but Google could not be reached to revoke it "
            f"({outcome.error}). You can also remove access at "
            "myaccount.google.com/permissions."
        )

    # ------------------------------------------------------------------ index
    def rescan_index(
        self,
        progress: Callable[[int, str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> list[ScanReport]:
        reports = self.index.scan_all(
            self.config.all_roots(), progress=progress, should_stop=should_stop
        )
        self._emit("changed")
        return reports

    def index_summary(self) -> dict[str, Any]:
        stats = self.index.stats()
        scans = [self.index.last_scan(r) for r in self.config.all_roots()]
        done = [s["finished_at"] for s in scans if s and s.get("finished_at")]
        return {**stats, "last_scan": max(done) if done else None, "roots": len(scans)}

    # ------------------------------------------------------------------ settings
    def set_ui_option(self, name: str, value: object) -> None:
        """Remember a look-and-feel choice at once (theme, folded menu, folded request cards).
        Nothing else changes and no copy is disturbed, so it is allowed while copies run, unlike
        ``apply_config``."""
        if name not in UI_OPTIONS:
            raise ValueError(f"{name} is not a window option")
        config = AppConfig.model_validate({**self.config.model_dump(), name: value})
        save_config(config, self._config_path)
        self.config = config

    # ------------------------------------------------------------------ who is using the app
    @property
    def user_name(self) -> str:
        """The signed-in person. It goes in the History beside what they approve."""
        return self._signed_in or "user"

    @property
    def signed_in(self) -> bool:
        return self._signed_in is not None

    def user_names(self) -> list[str]:
        return self.users.names()

    def last_user(self) -> str:
        """The person who used the app last (chosen first in the sign-in box)."""
        return self.config.user_name

    def sign_in(self, name: str, password: str) -> str:
        """Sign in with a name and password. Returns the name as stored."""
        stored = self.users.verify(name, password)
        if stored is None:
            self.repo.log("system", "sign_in_failed")  # no name: a typo is not a record
            raise ActionError("The name or the password is wrong")
        self._signed_in = stored
        self._remember_user(stored)
        return stored

    def create_user(self, name: str, password: str) -> str:
        """Add a new user and sign in as them."""
        try:
            stored = self.users.create(name, password)
        except UserError as exc:
            raise ActionError(str(exc)) from exc
        self._signed_in = stored
        self._remember_user(stored)
        return stored

    def sign_out(self) -> None:
        self._signed_in = None

    def _remember_user(self, name: str) -> None:
        try:
            self.set_ui_option("user_name", name)
        except (OSError, ValueError):
            log.warning("could not remember the last user")

    def _require_sign_in(self) -> None:
        if self.login_required and self._signed_in is None:
            raise ActionError("Sign in first")

    def set_appearance(self, appearance: str) -> None:
        self.set_ui_option("appearance", appearance)

    def apply_config(self, config: AppConfig, *, persist: bool = True) -> None:
        """Use new settings. Refused while copies run, so a job never changes folders midway."""
        if self.busy():
            raise ActionError("Wait for the running copies to finish before changing settings")
        removed = set(self.config.all_roots()) - set(config.all_roots())
        if persist:
            save_config(config, self._config_path)
        for root in removed:
            self.index.forget_root(root)
        self.config = config
        self.apply_dev()
        self.index.settings = config.index
        if self._smart is not None:
            self._smart.update(config.smart)
        self.queue.shutdown()
        self.queue = CopyQueue(
            CopyJobRunner(self.repo, config), concurrency=config.copy_options.concurrency
        )
        self._emit("changed")

    def set_dry_run(self, enabled: bool) -> None:
        self.apply_config(self.config.model_copy(update={"dry_run": enabled}))

    # ------------------------------------------------------------------ smart reading
    def smart_runtime(self) -> Runtime:
        """The local model (created when first needed; it is only started when it reads mail)."""
        if self._smart is None:
            root = self._llm_root or (data_dir() / "llm")
            self._smart = Runtime(
                Installer(root), self.config.smart, log_file=root.parent / "logs" / "llm.log"
            )
        return self._smart

    def _reader(self) -> SmartReader | None:
        if not self.config.smart.enabled:
            return None
        return SmartReader(self.smart_runtime(), self.repo, self.config.smart)

    def smart_status(self) -> Status:
        return self.smart_runtime().installer.status(self.config.smart)

    def smart_install(
        self,
        progress: Callable[[str, int, int], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        """Download the model and its program (large: run it off the GUI thread)."""
        try:
            self.smart_runtime().installer.install(
                self.config.smart, progress=progress, should_stop=should_stop
            )
        except SmartError as exc:
            raise ActionError(str(exc)) from exc

    def smart_test(self, subject: str, body: str) -> Reading:
        """Read a sample email with the model and show what it finds (nothing is saved)."""
        reader = SmartReader(self.smart_runtime(), self.repo, self.config.smart)
        try:
            return reader.read(subject, body)
        except SmartError as exc:
            raise ActionError(str(exc)) from exc

    def smart_running(self) -> bool:
        return self._smart is not None and self._smart.running()

    def smart_start(self) -> None:
        """Start the local model now, if it is installed and not already running."""
        try:
            self.smart_runtime().start()
        except SmartError as exc:
            raise ActionError(str(exc)) from exc

    def smart_stop(self) -> None:
        if self._smart is not None:
            self._smart.stop()

    def smart_forget_examples(self) -> int:
        """Delete everything the smart reader has learned from the person's decisions."""
        return self.repo.clear_examples()

    def assign_department(self, email_id: int, name: str) -> None:
        """An email from a sender in no department: say which department it is for. Its folders
        are then chosen the usual way and copied to that department's folder."""
        self._require_sign_in()
        email = self.repo.get_email(email_id)
        if email is None:
            raise ActionError("This request no longer exists")
        if email.get("department"):
            raise ActionError("This request already belongs to a department")
        dept = self._department(name)
        self.repo.set_email_department(email_id, dept.name)
        self._emit("changed")

    def _remember(self, email_id: int, is_request: bool, folders: list[str]) -> None:
        email = self.repo.get_email(email_id)
        if email is None:
            return
        text = f"Subject: {email.get('subject') or ''}\n\n{email.get('snippet') or ''}"
        self.repo.add_example(email_id, text, is_request, folders)

    def _smart_lines(self, email_id: int) -> list[dict[str, Any]]:
        rows = self.repo.requests_of_email(email_id)
        smart = []
        for row in rows:
            try:
                if json.loads(row.get("query_json") or "{}").get("source") == "smart":
                    smart.append(row)
            except ValueError:
                continue
        return smart

    def _learn_accepted(self, email_id: int) -> None:
        """The person approved a folder the model found: remember the names it got right."""
        if not self.config.smart.learn:
            return
        names = []
        for row in self._smart_lines(email_id):
            if row["state"] in ("APPROVED", "COPYING", "VERIFIED", "REPLIED"):
                item = json.loads(row["query_json"]).get("item", {})
                if item.get("program"):
                    names.append(str(item["program"]))
        if names:
            self._remember(email_id, True, names)

    def _learn_dismissed(self, email_id: int) -> None:
        """The person skipped or rejected everything the model found: it was not a request."""
        if not self.config.smart.learn:
            return
        smart = self._smart_lines(email_id)
        everything = self.repo.requests_of_email(email_id)
        if (
            smart
            and len(smart) == len(everything)
            and all(r["state"] in ("SKIPPED", "REJECTED") for r in smart)
        ):
            self._remember(email_id, False, [])

    # ------------------------------------------------------------------ history
    def export_history(self, path: Path, views: Iterable[RequestView] | None = None) -> int:
        """Write the history as CSV. Text that came from emails is defused against formulas."""
        rows = list(views if views is not None else self.views())
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(HISTORY_COLUMNS)
            for v in rows:
                writer.writerow(
                    [
                        v.id, v.received_at, csv_safe(f"{v.sender_name} <{v.sender_email}>"),
                        csv_safe(v.department), csv_safe(v.subject), csv_safe(v.requested),
                        v.state, csv_safe(v.source_path), csv_safe(v.destination_path),
                        v.file_count if v.file_count is not None else "",
                        v.total_bytes if v.total_bytes is not None else "",
                        f"{v.copy_seconds:.1f}" if v.copy_seconds is not None else "",
                        v.auth_status, csv_safe(v.failure_reason), csv_safe(v.approved_by),
                    ]
                )  # fmt: skip
        return len(rows)

    def shutdown(self) -> None:
        """Stop cleanly: running copies are cancelled and resume next time."""
        self.queue.shutdown(cancel=True)
        if self._smart is not None:
            self._smart.stop()

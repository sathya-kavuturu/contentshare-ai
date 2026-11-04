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

from contentshare.config.models import AppConfig
from contentshare.config.store import save_config
from contentshare.copy_engine import PreflightError, Progress, build_plan, format_bytes, preflight
from contentshare.gmail.auth import GmailAuth, required_scopes
from contentshare.gmail.errors import AuthRequiredError, GmailError
from contentshare.gmail.gateway import GmailGateway
from contentshare.gmail.intake import IntakeReport, IntakeService
from contentshare.gmail.replies import ReplyDraft, ReplyError, ReplyResult, ReplyService
from contentshare.index.store import ContentIndex, IndexedItem, ScanReport, measure
from contentshare.jobs.copy_job import (
    CopyJobRunner,
    JobOutcome,
    options_from_config,
    paths_overlap,
    resolve_source,
)
from contentshare.jobs.queue import CopyQueue
from contentshare.jobs.states import InvalidTransitionError, RequestState
from contentshare.matching.scorer import Candidate, rank_candidates
from contentshare.matching.service import destination_for
from contentshare.parsing.dates import date_phrases
from contentshare.parsing.request import RequestItem, parse_request
from contentshare.security.paths import PathSafetyError, resolve_within
from contentshare.storage.repository import Repository

log = logging.getLogger(__name__)

COPY_OK = "Copied and verified"  # the message of a successful ``copy_finished`` event

S = RequestState
GatewayFactory = Callable[[], GmailGateway]
MANUAL_RESULTS = 20


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
            item_id=data.get("item_id"),
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

    @property
    def verified_sender(self) -> bool:
        return self.auth_status == "verified"

    @property
    def size_text(self) -> str:
        return format_bytes(self.total_bytes) if self.total_bytes is not None else ""


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
    "destination", "files", "bytes", "sender_check", "failure",
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
    ) -> None:
        self.repo, self.index, self.config = repo, index, config
        self._gateway_factory, self.auth = gateway_factory, auth
        self._config_path, self._today = config_path, today
        self._live: dict[int, _Live] = {}
        self._lock = threading.Lock()
        self._check_lock = threading.Lock()
        self._listeners: list[Callable[[Event], None]] = []
        self.queue = CopyQueue(
            CopyJobRunner(repo, config), concurrency=config.copy_options.concurrency
        )

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
        for dept in self.config.departments:
            if dept.name == name:
                return dept
        raise ActionError(
            f"“{name or 'Unknown'}” is not a department in the settings; add it first"
        )

    def _transition(self, request_id: int, target: RequestState, **kwargs: Any) -> None:
        try:
            self.repo.transition(request_id, target, actor="user", **kwargs)
        except InvalidTransitionError as exc:
            raise ActionError(str(exc)) from exc
        except PermissionError as exc:
            raise ActionError(str(exc)) from exc

    def search(self, request_id: int, text: str) -> list[CandidateView]:
        """Manual search. The typed text is read like a request of its own (so typing a date or
        a language narrows the results), and the original email's date and language are not
        applied: the user may be looking for a different program altogether."""
        text = text.strip()
        if not text:
            return []
        self._row(request_id)  # the request must still exist
        parsed = parse_request(text, "", self.config.parsing, today=self._today())
        item = parsed.items[0] if parsed.items else RequestItem(program=text)
        if not item.program.strip():
            item = replace(item, program=text)
        hits = self.index.search(text, date_phrases=date_phrases(list(item.dates)))
        ranked = rank_candidates(
            item, hits, self.config.parsing, today=self._today(), limit=MANUAL_RESULTS
        )
        return [CandidateView.from_candidate(c) for c in ranked]

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
        for root in self.config.source_roots:
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
        if not dept.browse_anywhere:
            raise ActionError(
                "That is outside the storage folders in Settings. Choose something inside one "
                f"of them, or turn on “Allow choosing from anywhere” for {dept.name} in "
                "Settings → Departments."
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
                request_id, actor="user", reason="content changed by the user", **fields
            )
        self._emit("changed", request_id)

    def set_destination(self, request_id: int, path: str) -> None:
        row = self._row(request_id)
        if row["state"] not in (S.NEEDS_REVIEW, S.READY):
            raise ActionError("The destination can only be changed before approval")
        dept = self._department(row["department"])
        try:
            resolved = resolve_within(dept.destination_root, path.strip())
        except PathSafetyError as exc:
            raise ActionError(
                f"The destination must be inside {dept.destination_root}: {exc}"
            ) from exc
        self.repo.update_request(
            request_id, actor="user", reason="destination edited", destination_path=str(resolved)
        )
        self._emit("changed", request_id)

    def set_auto_reply(self, request_id: int, enabled: bool) -> None:
        try:
            self.repo.update_request(
                request_id, actor="user", reason="auto-reply toggled", auto_reply=int(enabled)
            )
        except InvalidTransitionError as exc:
            raise ActionError("This request is finished and cannot be changed") from exc

    def skip(self, request_id: int) -> None:
        self._transition(request_id, S.SKIPPED, reason="skipped by the user")
        self._emit("changed", request_id)

    def reject(self, request_id: int, reason: str = "") -> None:
        self._transition(request_id, S.REJECTED, reason=reason.strip()[:200] or "rejected")
        self._emit("changed", request_id)

    # ------------------------------------------------------------------ approving and copying
    def approve(
        self, request_id: int, *, confirmed: bool, acknowledge_unverified: bool = False
    ) -> ApproveResult:
        """Approve a READY request and start copying. Re-checks the source on disk first.
        In dry-run mode everything is checked and reported but nothing changes."""
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
            dst = resolve_within(dept.destination_root, row["destination_path"])
        except PathSafetyError as exc:
            raise ActionError(f"Path not allowed: {exc}") from exc
        if paths_overlap(src, dst):
            raise ActionError("The source and the destination overlap")

        note = self._recheck_on_disk(request_id, row, src)
        if self.config.dry_run:
            return self._dry_run(request_id, src, dst, note)

        self._transition(
            request_id, S.APPROVED, reason="approved by the user", auto_reply=row["auto_reply"]
        )
        self._submit(request_id)
        self._emit("changed", request_id)
        return ApproveResult(started=True, note=note)

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
        self.repo.log("user", "dry_run", request_id, files=len(plan.files), problems=len(problems))
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
            self._auto_reply(request_id)
        else:
            self._emit("copy_finished", request_id, outcome.error or "Copy did not finish")

    def _auto_reply(self, request_id: int) -> None:
        try:
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
        return ReplyService(self.repo, self._gateway_factory(), self.config, today=self._today)

    def check_now(
        self, *, refresh_index: bool = False, should_stop: Callable[[], bool] | None = None
    ) -> IntakeReport:
        # A manual click and the daily schedule must not read the same mailbox at once.
        if not self._check_lock.acquire(blocking=False):
            return IntakeReport(warnings=["A check is already running."], already_running=True)
        try:
            try:
                gateway = self._gateway_factory()
            except AuthRequiredError:
                return IntakeReport(needs_sign_in=True)
            service = IntakeService(gateway, self.repo, self.index, self.config, today=self._today)
            report = service.check_now(refresh_index=refresh_index, should_stop=should_stop)
        finally:
            self._check_lock.release()
        self._emit("changed")
        return report

    def index_is_stale(self, *, max_age_hours: int = 20) -> bool:
        """True if any storage folder was never scanned or not scanned recently."""
        if not self.config.source_roots:
            return False
        now = datetime.now(UTC)
        for root in self.config.source_roots:
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

    def rejection_draft(self, request_id: int, text: str | None = None) -> ReplyDraft:
        try:
            return self._replies().draft_rejection(request_id, text)
        except ReplyError as exc:
            raise ActionError(str(exc)) from exc

    def send_reply(self, draft: ReplyDraft) -> ReplyResult:
        if self.config.dry_run:
            raise ActionError("Dry-run mode is on: no email is sent. Turn it off to reply.")
        try:
            result = self._replies().send(draft)
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
            self.config.source_roots, progress=progress, should_stop=should_stop
        )
        self._emit("changed")
        return reports

    def index_summary(self) -> dict[str, Any]:
        stats = self.index.stats()
        scans = [self.index.last_scan(r) for r in self.config.source_roots]
        done = [s["finished_at"] for s in scans if s and s.get("finished_at")]
        return {**stats, "last_scan": max(done) if done else None, "roots": len(scans)}

    # ------------------------------------------------------------------ settings
    def set_appearance(self, appearance: str) -> None:
        """Remember the theme at once. Nothing else changes (and no copy is disturbed), so it is
        allowed while copies run, unlike ``apply_config``."""
        config = AppConfig.model_validate({**self.config.model_dump(), "appearance": appearance})
        save_config(config, self._config_path)
        self.config = config

    def apply_config(self, config: AppConfig, *, persist: bool = True) -> None:
        """Use new settings. Refused while copies run, so a job never changes folders midway."""
        if self.busy():
            raise ActionError("Wait for the running copies to finish before changing settings")
        removed = set(self.config.source_roots) - set(config.source_roots)
        if persist:
            save_config(config, self._config_path)
        for root in removed:
            self.index.forget_root(root)
        self.config = config
        self.index.settings = config.index
        self.queue.shutdown()
        self.queue = CopyQueue(
            CopyJobRunner(self.repo, config), concurrency=config.copy_options.concurrency
        )
        self._emit("changed")

    def set_dry_run(self, enabled: bool) -> None:
        self.apply_config(self.config.model_copy(update={"dry_run": enabled}))

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
                        v.auth_status, csv_safe(v.failure_reason),
                    ]
                )  # fmt: skip
        return len(rows)

    def shutdown(self) -> None:
        """Stop cleanly: running copies are cancelled and resume next time."""
        self.queue.shutdown(cancel=True)

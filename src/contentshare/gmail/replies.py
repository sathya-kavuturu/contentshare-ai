"""Sending replies, safely.

The rules, all enforced here and in the database:

* A "content shared" reply is possible only for a request whose copy was verified. A failed or
  unfinished copy can never produce one.
* It goes only to the address the request came from, in the same Gmail thread.
* The reply is journaled as SENDING before anything is sent, and a database rule allows only
  one SENDING/SENT reply per request, so a double click or a crash cannot send it twice.
* If the connection drops while sending, we do not guess. We look in the Gmail thread for the
  reply's tag: found means it was sent, absent means it was not, and if we cannot look the
  reply stays SENDING until we can (see ``recover_unfinished``).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, Literal

from contentshare.config.models import AppConfig
from contentshare.config.templates import REPLY_PLACEHOLDERS, render_template
from contentshare.copy_engine import format_bytes
from contentshare.gmail.errors import GmailError, TransientGmailError
from contentshare.gmail.gateway import GmailGateway
from contentshare.gmail.message import sanitize_display_name
from contentshare.gmail.reply import (
    MAX_BODY_CHARS,
    REPLY_TAG_HEADER,
    build_reply,
    encode_raw,
    reply_subject,
)
from contentshare.jobs.states import RequestState
from contentshare.storage.repository import Repository

log = logging.getLogger(__name__)

SHARED = "shared"
REJECTION = "rejection"
DEFAULT_REJECTION = (
    "Hello {requester_name},\n\n"
    "We are unable to share the content you requested at this time.\n"
    "Please get in touch if you would like to discuss it.\n"
)


class ReplyError(Exception):
    """A reply cannot be sent (wrong state, wrong recipient, empty text, ...)."""


@dataclass(frozen=True)
class ReplyDraft:
    request_id: int
    purpose: str  # "shared" | "rejection"
    to_addr: str
    subject: str
    body: str
    thread_id: str

    def with_body(self, body: str) -> ReplyDraft:
        """The user edited the text in the preview."""
        return replace(self, body=body)


@dataclass(frozen=True)
class ReplyResult:
    status: str  # "SENT" | "FAILED" | "UNCERTAIN"
    reply_id: int | None = None
    error: str | None = None
    gmail_message_id: str | None = None


@dataclass(frozen=True)
class SentLookup:
    state: Literal["found", "absent", "unknown"]
    message_id: str | None = None


class ReplyService:
    def __init__(
        self,
        repo: Repository,
        gateway: GmailGateway,
        config: AppConfig,
        *,
        today: Callable[[], date] = date.today,
        new_token: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self.repo, self.gateway, self.config = repo, gateway, config
        self._today, self._new_token = today, new_token

    # ------------------------------------------------------------------ drafting
    def draft_shared(self, request_id: int) -> ReplyDraft:
        row = self._request(request_id)
        if row["state"] != RequestState.VERIFIED:
            raise ReplyError(
                f"The content has not been verified (request is {row['state']}); "
                "a “shared” reply cannot be sent."
            )
        template = self._department_template(row["department"])
        body = render_template(template, self._values(row), REPLY_PLACEHOLDERS)
        return self._draft(row, SHARED, body)

    def draft_rejection(self, request_id: int, text: str | None = None) -> ReplyDraft:
        row = self._request(request_id)
        if row["state"] != RequestState.REJECTED:
            raise ReplyError("Only a rejected request can get a rejection reply")
        body = (
            text.strip()
            if text and text.strip()
            else render_template(DEFAULT_REJECTION, self._values(row), REPLY_PLACEHOLDERS)
        )
        return self._draft(row, REJECTION, body)

    def _draft(self, row: dict[str, Any], purpose: str, body: str) -> ReplyDraft:
        return ReplyDraft(
            request_id=int(row["id"]),
            purpose=purpose,
            to_addr=str(row["sender_email"]),
            subject=reply_subject(str(row["subject"])),
            body=body,
            thread_id=str(row["gmail_thread_id"]),
        )

    def _department_template(self, department: str | None) -> str:
        for dept in self.config.departments:
            if dept.name == department and dept.reply_template:
                return dept.reply_template
        return self.config.default_reply_template

    def _values(self, row: dict[str, Any]) -> dict[str, str]:
        detail = json.loads(row.get("query_json") or "{}").get("item", {})
        dates = detail.get("dates") or []
        source = str(row.get("source_path") or "")
        return {
            "requester_name": sanitize_display_name(str(row.get("sender_name") or "")) or "there",
            "program": str(detail.get("program") or "").strip() or Path(source).name,
            "destination_path": str(row.get("destination_path") or ""),
            "file_count": str(row.get("file_count") or 0),
            "total_size": format_bytes(row.get("total_bytes") or 0),
            "date": str(dates[0][0]) if dates else self._today().isoformat(),
        }

    def _request(self, request_id: int) -> dict[str, Any]:
        row = self.repo.get_request(request_id)
        if row is None:
            raise ReplyError(f"No request {request_id}")
        return row

    # ------------------------------------------------------------------ sending
    def send(self, draft: ReplyDraft, *, actor: str = "user") -> ReplyResult:
        row = self._request(draft.request_id)
        self._check_sendable(draft, row)
        email = self.repo.get_email(int(row["email_id"]))
        if email is None:
            raise ReplyError("The original email is missing")

        token = self._new_token()
        try:
            reply_id = self.repo.begin_reply(
                draft.request_id, purpose=draft.purpose, to_addr=draft.to_addr,
                body=draft.body, token=token,
            )  # fmt: skip
        except sqlite3.IntegrityError as exc:
            raise ReplyError(
                "A reply for this request has already been sent or is being sent"
            ) from exc

        message = build_reply(
            to_addr=draft.to_addr,
            subject=draft.subject,
            body=draft.body,
            token=token,
            original_message_id=email.get("rfc_message_id"),
            original_references=email.get("references_header"),
        )
        try:
            sent_id = self.gateway.send_raw(encode_raw(message), draft.thread_id)
        except TransientGmailError as exc:
            log.warning("send interrupted for reply %s: %s", reply_id, exc)
            return self._settle_uncertain(reply_id, draft.thread_id, token, str(exc))
        except GmailError as exc:  # refused: certainly not sent
            self.repo.finish_reply(reply_id, sent=False, error=str(exc))
            return ReplyResult("FAILED", reply_id, str(exc))
        self.repo.finish_reply(reply_id, sent=True, gmail_message_id=sent_id)
        self.repo.log(actor, "reply_confirmed", draft.request_id, reply_id=reply_id)
        return ReplyResult("SENT", reply_id, gmail_message_id=sent_id)

    def _check_sendable(self, draft: ReplyDraft, row: dict[str, Any]) -> None:
        """Re-check everything against the database: the draft is only a proposal."""
        if draft.purpose == SHARED and row["state"] != RequestState.VERIFIED:
            raise ReplyError("The content has not been verified; no “shared” reply is allowed")
        if draft.purpose == REJECTION and row["state"] != RequestState.REJECTED:
            raise ReplyError("Only a rejected request can get a rejection reply")
        if draft.purpose not in {SHARED, REJECTION}:
            raise ReplyError("Unknown kind of reply")
        if draft.to_addr.lower() != str(row["sender_email"]).lower():
            raise ReplyError("A reply may only go to the person who made the request")
        if not draft.body.strip():
            raise ReplyError("The reply is empty")
        if len(draft.body) > MAX_BODY_CHARS:
            raise ReplyError("The reply is too long")

    def _settle_uncertain(
        self, reply_id: int, thread_id: str, token: str, error: str
    ) -> ReplyResult:
        lookup = self._find_sent(thread_id, token)
        if lookup.state == "unknown":  # could not look: stay SENDING and resolve later
            return ReplyResult(
                "UNCERTAIN", reply_id, "Could not confirm whether the reply was sent"
            )
        if lookup.state == "found":
            self.repo.finish_reply(reply_id, sent=True, gmail_message_id=lookup.message_id)
            return ReplyResult("SENT", reply_id, gmail_message_id=lookup.message_id)
        self.repo.finish_reply(reply_id, sent=False, error=f"Not sent: {error}")
        return ReplyResult("FAILED", reply_id, f"Not sent: {error}")

    def _find_sent(self, thread_id: str, token: str) -> SentLookup:
        """Is the reply carrying ``token`` in the Gmail thread's sent mail?"""
        try:
            messages = self.gateway.thread_headers(thread_id, [REPLY_TAG_HEADER])
        except GmailError:
            return SentLookup("unknown")
        for message in messages:
            tagged = message["headers"].get(REPLY_TAG_HEADER.lower()) == token
            if tagged and "SENT" in message["labels"]:
                return SentLookup("found", str(message["id"]))
        return SentLookup("absent")

    # ------------------------------------------------------------------ recovery & auto
    def recover_unfinished(self) -> list[ReplyResult]:
        """Settle replies left in SENDING by a crash or a dropped connection."""
        results = []
        for reply in self.repo.list_replies(status="SENDING"):
            row = self.repo.get_request(int(reply["request_id"]))
            if row is None or not reply.get("token"):
                continue
            lookup = self._find_sent(str(row["gmail_thread_id"]), str(reply["token"]))
            reply_id = int(reply["id"])
            if lookup.state == "unknown":
                results.append(ReplyResult("UNCERTAIN", reply_id, "Gmail could not be reached"))
            elif lookup.state == "found":
                self.repo.finish_reply(reply_id, sent=True, gmail_message_id=lookup.message_id)
                results.append(ReplyResult("SENT", reply_id, gmail_message_id=lookup.message_id))
            else:
                msg = "The app stopped before this reply was sent"
                self.repo.finish_reply(reply_id, sent=False, error=msg)
                results.append(ReplyResult("FAILED", reply_id, msg))
        return results

    def maybe_auto_reply(self, request_id: int) -> ReplyResult | None:
        """Send the standard reply if the user asked for that on this request and it is
        verified. Returns ``None`` when there is nothing to do."""
        row = self._request(request_id)
        if not row["auto_reply"] or row["state"] != RequestState.VERIFIED:
            return None
        if self.repo.list_replies(request_id, "SENT") or self.repo.list_replies(
            request_id, "SENDING"
        ):
            return None
        return self.send(self.draft_shared(request_id), actor="system")

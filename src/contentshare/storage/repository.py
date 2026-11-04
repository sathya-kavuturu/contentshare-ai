"""Data access for emails, requests and the audit log.

State changes go through :meth:`Repository.transition`, which validates the move and writes
the audit record in the same transaction, so the database can never show a state without
its history.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from contentshare.jobs.states import (
    TERMINAL,
    InvalidTransitionError,
    RequestState,
    check_transition,
)
from contentshare.storage.db import Database

SNIPPET_MAX = 2000
# Columns callers may set alongside a transition. Names are interpolated into SQL, so this
# whitelist is what keeps that safe.
_UPDATABLE = frozenset(
    {
        "source_path",
        "confidence",
        "file_count",
        "total_bytes",
        "destination_path",
        "auto_reply",
        "failure_reason",
        "query_json",
    }
)


_REQUEST_SELECT = (
    "SELECT r.*, e.sender_email, e.sender_name, e.department, e.subject, e.snippet, "
    "e.received_at, e.auth_status, e.gmail_thread_id, e.gmail_message_id, "
    # how long the last finished copy of this request took, in seconds
    "(SELECT (julianday(j.finished_at) - julianday(j.started_at)) * 86400.0 FROM jobs j "
    "WHERE j.request_id = r.id AND j.finished_at IS NOT NULL ORDER BY j.id DESC LIMIT 1) "
    "AS copy_seconds "
    "FROM requests r JOIN emails e ON e.id = r.email_id"
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _now_precise() -> str:
    """For timing a copy: short copies take less than the second ``_now`` rounds to."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class Repository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # --- audit -----------------------------------------------------------------
    @staticmethod
    def _audit(
        conn: sqlite3.Connection,
        actor: str,
        event: str,
        request_id: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO audit_log (ts, actor, event, request_id, detail) VALUES (?, ?, ?, ?, ?)",
            (_now(), actor, event, request_id, json.dumps(detail or {}, default=str)),
        )

    def log(self, actor: str, event: str, request_id: int | None = None, **detail: Any) -> None:
        with self.db.transaction() as conn:
            self._audit(conn, actor, event, request_id, detail)

    def last_event_time(self, event: str) -> str | None:
        """Timestamp (ISO, UTC) of the most recent audit entry with this event name."""
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT ts FROM audit_log WHERE event = ? ORDER BY id DESC LIMIT 1", (event,)
            ).fetchone()
        return str(row["ts"]) if row else None

    def audit_entries(self, request_id: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM audit_log"
        args: tuple[Any, ...] = ()
        if request_id is not None:
            sql += " WHERE request_id = ?"
            args = (request_id,)
        with self.db.read() as conn:
            return [dict(r) for r in conn.execute(sql + " ORDER BY id", args)]

    # --- emails ----------------------------------------------------------------
    def is_processed(self, gmail_message_id: str) -> bool:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT 1 FROM emails WHERE gmail_message_id = ?", (gmail_message_id,)
            ).fetchone()
        return row is not None

    def add_email(
        self,
        *,
        gmail_message_id: str,
        gmail_thread_id: str,
        sender_email: str,
        received_at: str,
        auth_status: str,
        sender_name: str = "",
        department: str | None = None,
        subject: str = "",
        snippet: str = "",
        ignored_reason: str | None = None,
        rfc_message_id: str | None = None,
        references_header: str | None = None,
    ) -> int | None:
        """Record an email. Returns its id, or ``None`` if this message was already recorded."""
        with self.db.transaction() as conn:
            cur = conn.execute(
                # ON CONFLICT only skips a duplicate message id; unlike OR IGNORE it still
                # raises on CHECK/NOT NULL violations instead of silently dropping the email.
                "INSERT INTO emails (gmail_message_id, gmail_thread_id, sender_email, "
                "sender_name, department, subject, snippet, received_at, auth_status, "
                "ignored_reason, created_at, rfc_message_id, references_header) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(gmail_message_id) DO NOTHING",
                (
                    gmail_message_id,
                    gmail_thread_id,
                    sender_email.lower(),
                    sender_name,
                    department,
                    subject[:300],
                    snippet[:SNIPPET_MAX],
                    received_at,
                    auth_status,
                    ignored_reason,
                    _now(),
                    rfc_message_id,
                    references_header,
                ),
            )
            if cur.rowcount == 0:
                return None
            email_id = int(cur.lastrowid or 0)
            self._audit(
                conn,
                "system",
                "email_ignored" if ignored_reason else "email_recorded",
                detail={
                    "email_id": email_id,
                    "message_id": gmail_message_id,
                    "reason": ignored_reason,
                    "auth": auth_status,
                },
            )
            return email_id

    def get_email(self, email_id: int) -> dict[str, Any] | None:
        with self.db.read() as conn:
            return _row(conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone())

    # --- requests --------------------------------------------------------------
    def add_request(
        self, email_id: int, line_no: int, query: dict[str, Any], *, auto_reply: bool = False
    ) -> int:
        now = _now()
        with self.db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO requests (email_id, line_no, query_json, auto_reply, state, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (email_id, line_no, json.dumps(query), int(auto_reply), RequestState.NEW, now, now),
            )
            request_id = int(cur.lastrowid or 0)
            # Only ids go in the audit log: what the email said stays out of it.
            self._audit(
                conn,
                "system",
                "request_created",
                request_id,
                {"email_id": email_id, "line": line_no},
            )
            return request_id

    def get_request(self, request_id: int) -> dict[str, Any] | None:
        with self.db.read() as conn:
            return _row(conn.execute(_REQUEST_SELECT + " WHERE r.id = ?", (request_id,)).fetchone())

    def requests_of_email(self, email_id: int) -> list[dict[str, Any]]:
        """Every line of one email, in the order they were asked for."""
        with self.db.read() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    _REQUEST_SELECT + " WHERE r.email_id = ? ORDER BY r.line_no", (email_id,)
                )
            ]

    def list_requests(self, states: Iterable[RequestState] | None = None) -> list[dict[str, Any]]:
        sql = _REQUEST_SELECT
        args: list[Any] = []
        if states is not None:
            wanted = [str(s) for s in states]
            if not wanted:
                return []
            sql += f" WHERE r.state IN ({','.join('?' * len(wanted))})"
            args = wanted
        with self.db.read() as conn:
            return [dict(r) for r in conn.execute(sql + " ORDER BY r.id", args)]

    def transition(
        self,
        request_id: int,
        target: RequestState,
        *,
        actor: str,
        reason: str | None = None,
        **fields: Any,
    ) -> None:
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise ValueError(f"Cannot set column(s): {', '.join(sorted(unknown))}")
        with self.db.transaction() as conn:
            row = conn.execute("SELECT state FROM requests WHERE id = ?", (request_id,)).fetchone()
            if row is None:
                raise KeyError(f"No request {request_id}")
            current = RequestState(row["state"])
            check_transition(current, target)
            if current == RequestState.NEW and target == RequestState.READY:
                email = conn.execute(
                    "SELECT e.auth_status FROM emails e JOIN requests r ON r.email_id = e.id "
                    "WHERE r.id = ?",
                    (request_id,),
                ).fetchone()
                if email["auth_status"] != "verified":
                    raise PermissionError(
                        "A request from an unverified sender must go through NEEDS_REVIEW first"
                    )
            assignments = ", ".join(f"{col} = ?" for col in fields)  # whitelisted names only
            sql = "UPDATE requests SET state = ?, updated_at = ?"
            if assignments:
                sql += ", " + assignments
            sql += " WHERE id = ?"
            conn.execute(sql, (str(target), _now(), *fields.values(), request_id))
            self._audit(
                conn,
                actor,
                "state_changed",
                request_id,
                {"from": str(current), "to": str(target), "reason": reason, "fields": list(fields)},
            )

    def update_request(
        self, request_id: int, *, actor: str, reason: str | None = None, **fields: Any
    ) -> None:
        """Change columns without changing the state (e.g. the user edits the destination).
        Audited. A request that reached a terminal state is never edited."""
        unknown = set(fields) - _UPDATABLE
        if unknown or not fields:
            raise ValueError(f"Cannot set column(s): {', '.join(sorted(unknown)) or '(none)'}")
        with self.db.transaction() as conn:
            row = conn.execute("SELECT state FROM requests WHERE id = ?", (request_id,)).fetchone()
            if row is None:
                raise KeyError(f"No request {request_id}")
            if RequestState(row["state"]) in TERMINAL:
                raise InvalidTransitionError(RequestState(row["state"]), RequestState(row["state"]))
            assignments = ", ".join(f"{col} = ?" for col in fields)  # whitelisted names only
            conn.execute(
                f"UPDATE requests SET updated_at = ?, {assignments} WHERE id = ?",  # noqa: S608  # nosec B608
                (_now(), *fields.values(), request_id),
            )
            self._audit(
                conn, actor, "request_edited", request_id,
                {"reason": reason, "fields": list(fields)},
            )  # fmt: skip

    def recover_interrupted(self) -> list[int]:
        """After a crash, requests stuck in COPYING go back to APPROVED so the job resumes."""
        with self.db.transaction() as conn:
            ids = [
                int(r["id"])
                for r in conn.execute(
                    "SELECT id FROM requests WHERE state = ?", (RequestState.COPYING,)
                )
            ]
            for request_id in ids:
                conn.execute(
                    "UPDATE requests SET state = ?, updated_at = ? WHERE id = ?",
                    (RequestState.APPROVED, _now(), request_id),
                )
                self._audit(
                    conn,
                    "system",
                    "recovered_after_crash",
                    request_id,
                    {"from": "COPYING", "to": "APPROVED"},
                )
            return ids

    # --- copy jobs ---------------------------------------------------------------
    def create_job(self, request_id: int) -> tuple[int, int]:
        """Start a job record. Returns ``(job_id, attempt number for this request)``."""
        with self.db.transaction() as conn:
            attempt = (
                conn.execute(
                    "SELECT COUNT(*) FROM jobs WHERE request_id = ?", (request_id,)
                ).fetchone()[0]
                + 1
            )
            cur = conn.execute(
                "INSERT INTO jobs (request_id, state, attempt, started_at) "
                "VALUES (?, 'RUNNING', ?, ?)",
                (request_id, attempt, _now_precise()),
            )
            return int(cur.lastrowid or 0), int(attempt)

    def finish_job(self, job_id: int, state: str, error: str | None = None) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE jobs SET state = ?, finished_at = ?, error = ? WHERE id = ?",
                (state, _now_precise(), error, job_id),
            )

    def record_file(
        self,
        job_id: int,
        *,
        rel_path: str,
        size: int,
        mtime_ns: int,
        algorithm: str,
        state: str,
        hash_hex: str | None,
        attempts: int,
        error: str | None,
    ) -> None:
        """Journal one file's result right after it finishes, so a crash keeps what is done."""
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO job_files (job_id, rel_path, size, mtime_ns, hash_algo, source_hash, "
                "dest_hash, state, attempts, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(job_id, rel_path) DO UPDATE SET size = excluded.size, "
                "mtime_ns = excluded.mtime_ns, source_hash = excluded.source_hash, "
                "dest_hash = excluded.dest_hash, state = excluded.state, "
                "attempts = excluded.attempts, error = excluded.error",
                (
                    job_id,
                    rel_path,
                    size,
                    mtime_ns,
                    algorithm,
                    hash_hex,
                    hash_hex,
                    state,
                    attempts,
                    error,
                ),
            )

    def verified_files(self, request_id: int, algorithm: str) -> dict[str, tuple[int, int, str]]:
        """Files an earlier job of this request verified: ``rel -> (size, mtime_ns, hash)``.
        Only the same algorithm counts, and later jobs override earlier ones."""
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT f.rel_path, f.size, f.mtime_ns, f.dest_hash FROM job_files f "
                "JOIN jobs j ON j.id = f.job_id WHERE j.request_id = ? AND f.state = 'VERIFIED' "
                "AND f.hash_algo = ? AND f.mtime_ns IS NOT NULL AND f.dest_hash IS NOT NULL "
                "ORDER BY f.id",
                (request_id, algorithm),
            ).fetchall()
        return {r["rel_path"]: (r["size"], r["mtime_ns"], r["dest_hash"]) for r in rows}

    def job_files(self, job_id: int) -> list[dict[str, Any]]:
        with self.db.read() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM job_files WHERE job_id = ? ORDER BY rel_path", (job_id,)
                )
            ]

    def list_jobs(self, request_id: int) -> list[dict[str, Any]]:
        with self.db.read() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM jobs WHERE request_id = ? ORDER BY id", (request_id,)
                )
            ]

    # --- replies ---------------------------------------------------------------
    def begin_reply(
        self,
        request_id: int,
        *,
        purpose: str,
        to_addr: str,
        body: str,
        token: str,
    ) -> int:
        """Journal a reply BEFORE sending it. The unique index on (request, purpose) for
        SENDING/SENT rows means a second click, or a second process, cannot send it twice."""
        with self.db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO replies (request_id, purpose, to_addr, body, token, status, "
                "created_at) VALUES (?, ?, ?, ?, ?, 'SENDING', ?)",
                (request_id, purpose, to_addr, body, token, _now()),
            )
            reply_id = int(cur.lastrowid or 0)
            self._audit(
                conn,
                "system",
                "reply_sending",
                request_id,
                {"reply_id": reply_id, "purpose": purpose},
            )
            return reply_id

    def finish_reply(
        self,
        reply_id: int,
        *,
        sent: bool,
        gmail_message_id: str | None = None,
        error: str | None = None,
    ) -> None:
        """Close a journaled reply. A successful 'shared' reply also moves the request
        VERIFIED -> REPLIED in the same transaction, so the two can never disagree."""
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM replies WHERE id = ?", (reply_id,)).fetchone()
            if row is None:
                raise KeyError(f"No reply {reply_id}")
            request_id = int(row["request_id"])
            conn.execute(
                "UPDATE replies SET status = ?, gmail_message_id = ?, error = ?, sent_at = ? "
                "WHERE id = ?",
                (
                    "SENT" if sent else "FAILED",
                    gmail_message_id,
                    error,
                    _now() if sent else None,
                    reply_id,
                ),
            )
            current = RequestState(
                conn.execute("SELECT state FROM requests WHERE id = ?", (request_id,)).fetchone()[0]
            )
            if sent and row["purpose"] == "shared" and current != RequestState.REPLIED:
                check_transition(current, RequestState.REPLIED)
                conn.execute(
                    "UPDATE requests SET state = ?, updated_at = ? WHERE id = ?",
                    (RequestState.REPLIED, _now(), request_id),
                )
                self._audit(
                    conn, "system", "state_changed", request_id,
                    {"from": str(current), "to": "REPLIED", "reason": "reply sent"},
                )  # fmt: skip
            self._audit(
                conn, "system", "reply_sent" if sent else "reply_failed", request_id,
                {"reply_id": reply_id, "error": error},
            )  # fmt: skip

    def get_reply(self, reply_id: int) -> dict[str, Any] | None:
        with self.db.read() as conn:
            return _row(conn.execute("SELECT * FROM replies WHERE id = ?", (reply_id,)).fetchone())

    def list_replies(
        self, request_id: int | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM replies WHERE 1 = 1"
        args: list[Any] = []
        if request_id is not None:
            sql += " AND request_id = ?"
            args.append(request_id)
        if status is not None:
            sql += " AND status = ?"
            args.append(status)
        with self.db.read() as conn:
            return [dict(r) for r in conn.execute(sql + " ORDER BY id", args)]

    def thread_known(self, gmail_thread_id: str) -> bool:
        """True if we have already recorded an email from this Gmail conversation."""
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT 1 FROM emails WHERE gmail_thread_id = ? LIMIT 1", (gmail_thread_id,)
            ).fetchone()
        return row is not None

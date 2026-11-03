from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from contentshare.jobs.states import InvalidTransitionError, RequestState
from contentshare.storage.db import MIGRATIONS, Database
from contentshare.storage.repository import SNIPPET_MAX, Repository

S = RequestState


def _email(repo: Repository, msg: str = "m1", auth: str = "verified") -> int:
    email_id = repo.add_email(
        gmail_message_id=msg,
        gmail_thread_id="t1",
        sender_email="Anita@Dept.org",
        sender_name="Anita R.",
        department="Media Team",
        subject="Please share Program X",
        snippet="x" * 2000,
        received_at="2026-11-03T08:41:00+00:00",
        auth_status=auth,
    )
    assert email_id is not None
    return email_id


def test_migrations_create_schema_and_are_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "a.db"
    db = Database(path)
    assert db.migrate() == len(MIGRATIONS)
    with db.read() as conn:
        tables = {
            r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"emails", "requests", "jobs", "job_files", "replies", "audit_log"} <= tables
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    Database(path)  # reopening must not fail or re-run migrations


def test_newer_database_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "a.db"
    Database(path)
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 5}")
    conn.close()
    with pytest.raises(RuntimeError, match="Update ContentShare"):
        Database(path)


def test_same_email_is_never_processed_twice(repo: Repository) -> None:
    first = _email(repo)
    assert repo.is_processed("m1")
    assert not repo.is_processed("m2")
    duplicate = repo.add_email(
        gmail_message_id="m1",
        gmail_thread_id="t1",
        sender_email="a@b.org",
        received_at="2026-11-03T09:00:00+00:00",
        auth_status="verified",
    )
    assert first is not None
    assert duplicate is None


def test_email_fields_are_normalised_and_capped(repo: Repository) -> None:
    email = repo.get_email(_email(repo))
    assert email is not None
    assert email["sender_email"] == "anita@dept.org"
    assert len(email["snippet"]) == SNIPPET_MAX


def test_invalid_auth_status_rejected(repo: Repository) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _email(repo, "bad", auth="trusted-ish")


def test_request_lifecycle_with_audit(repo: Repository) -> None:
    rid = repo.add_request(_email(repo), 1, {"program": "Program X"})
    repo.transition(rid, S.READY, actor="system", source_path=r"\\s\p\X", confidence=92.5)
    repo.transition(rid, S.APPROVED, actor="user", destination_path=r"\\nas\in\X", auto_reply=True)
    repo.transition(rid, S.COPYING, actor="system")
    repo.transition(rid, S.VERIFIED, actor="system", file_count=37, total_bytes=48_200_000_000)
    repo.transition(rid, S.REPLIED, actor="user")

    row = repo.get_request(rid)
    assert row is not None
    assert row["state"] == "REPLIED"
    assert row["source_path"] == r"\\s\p\X"
    assert row["auto_reply"] == 1

    trail = [(e["actor"], e["event"]) for e in repo.audit_entries(rid)]
    assert trail[0] == ("system", "request_created")
    assert trail.count(("user", "state_changed")) == 2
    assert len(trail) == 6


def test_invalid_transition_leaves_state_and_audit_untouched(repo: Repository) -> None:
    rid = repo.add_request(_email(repo), 1, {})
    before = len(repo.audit_entries(rid))
    with pytest.raises(InvalidTransitionError):
        repo.transition(rid, S.COPYING, actor="user")
    row = repo.get_request(rid)
    assert row is not None
    assert row["state"] == "NEW"
    assert len(repo.audit_entries(rid)) == before


def test_suspicious_sender_cannot_skip_review(repo: Repository) -> None:
    rid = repo.add_request(_email(repo, "s1", auth="suspicious"), 1, {})
    with pytest.raises(PermissionError):
        repo.transition(rid, S.READY, actor="system")
    repo.transition(rid, S.NEEDS_REVIEW, actor="system")
    repo.transition(rid, S.READY, actor="user")  # a human has now looked at it


def test_transition_rejects_unknown_columns(repo: Repository) -> None:
    rid = repo.add_request(_email(repo), 1, {})
    with pytest.raises(ValueError, match="Cannot set column"):
        repo.transition(rid, S.READY, actor="system", **{"state = 'REPLIED', id": 1})


def test_transition_unknown_request(repo: Repository) -> None:
    with pytest.raises(KeyError):
        repo.transition(999, S.READY, actor="user")


def test_failed_transaction_rolls_back_everything(repo: Repository) -> None:
    rid = repo.add_request(_email(repo), 1, {})
    with pytest.raises(sqlite3.Error), repo.db.transaction() as conn:
        conn.execute("UPDATE requests SET state = 'READY' WHERE id = ?", (rid,))
        conn.execute("INSERT INTO no_such_table VALUES (1)")
    row = repo.get_request(rid)
    assert row is not None
    assert row["state"] == "NEW"


def test_audit_log_is_append_only(repo: Repository) -> None:
    repo.log("user", "settings_changed", detail_key="x")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"), repo.db.transaction() as conn:
        conn.execute("UPDATE audit_log SET event = 'tampered'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"), repo.db.transaction() as conn:
        conn.execute("DELETE FROM audit_log")


def test_list_requests_filters_by_state(repo: Repository) -> None:
    email_id = _email(repo)
    a = repo.add_request(email_id, 1, {"n": 1})
    b = repo.add_request(email_id, 2, {"n": 2})
    repo.transition(a, S.READY, actor="system")
    assert [r["id"] for r in repo.list_requests([S.READY])] == [a]
    assert [r["id"] for r in repo.list_requests([S.NEW])] == [b]
    assert len(repo.list_requests()) == 2
    assert repo.list_requests([]) == []
    assert repo.list_requests([S.NEW])[0]["sender_email"] == "anita@dept.org"


def test_one_request_line_per_item_is_unique(repo: Repository) -> None:
    email_id = _email(repo)
    repo.add_request(email_id, 1, {})
    with pytest.raises(sqlite3.IntegrityError):
        repo.add_request(email_id, 1, {})


def test_crash_recovery_moves_copying_back_to_approved(repo: Repository) -> None:
    email_id = _email(repo)
    stuck = repo.add_request(email_id, 1, {})
    fine = repo.add_request(email_id, 2, {})
    for rid in (stuck, fine):
        repo.transition(rid, S.READY, actor="system")
        repo.transition(rid, S.APPROVED, actor="user")
    repo.transition(stuck, S.COPYING, actor="system")

    assert repo.recover_interrupted() == [stuck]
    row = repo.get_request(stuck)
    assert row is not None
    assert row["state"] == "APPROVED"
    assert repo.recover_interrupted() == []
    assert any(e["event"] == "recovered_after_crash" for e in repo.audit_entries(stuck))


def test_concurrent_writers_do_not_lose_rows(repo: Repository) -> None:
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(10):
                repo.log("system", "tick", worker=n, i=i)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(repo.audit_entries()) == 40

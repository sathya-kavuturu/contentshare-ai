"""Job runner + queue: path enforcement, state changes, journal, crash-resume, no false replies."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

import contentshare.jobs.copy_job as mod
from contentshare.config.models import AppConfig, Department
from contentshare.copy_engine import MIB, Control
from contentshare.copy_engine.models import Hooks
from contentshare.jobs.copy_job import CopyJobRunner, JobOutcome, options_from_config
from contentshare.jobs.queue import CopyQueue
from contentshare.jobs.states import InvalidTransitionError, RequestState
from contentshare.storage.db import MIGRATIONS, Database
from contentshare.storage.repository import Repository

S = RequestState


@pytest.fixture
def world(tmp_path: Path, repo: Repository):  # type: ignore[no-untyped-def]
    storage = tmp_path / "storage"
    media = tmp_path / "media_incoming"
    storage.mkdir()
    media.mkdir()
    program = storage / "2026-10-12_ProgramX_EN"
    (program / "sub").mkdir(parents=True)
    (program / "video.mxf").write_bytes(os.urandom(2 * MIB + 5))
    (program / "sub" / "notes.txt").write_bytes(b"notes")
    config = AppConfig(
        source_roots=[str(storage)],
        departments=[
            {
                "name": "Media Team",
                "sender_emails": ["anita@dept.org"],
                "destination_root": str(media),
            }
        ],
        copy_options={"chunk_mb": 1, "max_attempts": 2},
    )
    runner = CopyJobRunner(repo, config, sleep=lambda _s: None)
    return storage, media, program, config, runner


def approved_request(repo: Repository, src: Path, dst: Path, msg: str = "m1") -> int:
    email_id = repo.add_email(
        gmail_message_id=msg, gmail_thread_id="t", sender_email="anita@dept.org",
        received_at="2026-11-03T08:00:00+00:00", auth_status="verified", department="Media Team",
    )  # fmt: skip
    assert email_id is not None
    rid = repo.add_request(email_id, 1, {})
    repo.transition(rid, S.READY, actor="system", source_path=str(src), confidence=95.0)
    repo.transition(rid, S.APPROVED, actor="user", destination_path=str(dst))
    return rid


def state(repo: Repository, rid: int) -> str:
    row = repo.get_request(rid)
    assert row is not None
    return str(row["state"])


def test_successful_job_verifies_and_journals(world, repo: Repository) -> None:  # type: ignore[no-untyped-def]
    storage, media, program, config, runner = world
    dst = media / "ProgramX"
    rid = approved_request(repo, program, dst)

    outcome = runner.run(rid)

    assert outcome.state == S.VERIFIED
    assert state(repo, rid) == "VERIFIED"
    row = repo.get_request(rid)
    assert row is not None
    assert row["file_count"] == 2
    assert row["total_bytes"] == 2 * MIB + 5 + 5
    assert (dst / "video.mxf").read_bytes() == (program / "video.mxf").read_bytes()
    assert not (dst / "ProgramX.checksums.xxhash").exists()  # only when the setting asks for it

    jobs = repo.list_jobs(rid)
    assert [(j["state"], j["attempt"]) for j in jobs] == [("VERIFIED", 1)]
    files = repo.job_files(jobs[0]["id"])
    assert {f["rel_path"] for f in files} == {"video.mxf", "sub/notes.txt"}
    assert all(f["state"] == "VERIFIED" and f["dest_hash"] and f["source_hash"] for f in files)
    events = [e["event"] for e in repo.audit_entries(rid)]
    assert "copy_started" in events
    assert "copy_verified" in events


def test_failed_copy_marks_failed_and_never_verified(
    world, repo: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    storage, media, program, config, runner = world
    rid = approved_request(repo, program, media / "ProgramX")
    real_copy_tree = mod.copy_tree

    def corrupting(plan, options, control, **kw):  # type: ignore[no-untyped-def]
        def corrupt(tmp: Path) -> None:
            if tmp.name.startswith("notes"):
                tmp.write_bytes(b"CORRUPT")

        return real_copy_tree(plan, options, control, hooks=Hooks(after_write=corrupt), **kw)

    monkeypatch.setattr(mod, "copy_tree", corrupting)
    outcome = runner.run(rid)

    assert outcome.state == S.FAILED
    assert state(repo, rid) == "FAILED"
    row = repo.get_request(rid)
    assert row is not None
    assert "notes.txt" in row["failure_reason"]
    assert not (media / "ProgramX" / "sub" / "notes.txt").exists()
    # FAILED can never jump to VERIFIED/REPLIED: the "shared" reply is impossible
    for bad in (S.VERIFIED, S.REPLIED):
        with pytest.raises(InvalidTransitionError):
            repo.transition(rid, bad, actor="user")


def test_retry_after_failure_only_redoes_what_failed(world, repo: Repository) -> None:  # type: ignore[no-untyped-def]
    storage, media, program, config, runner = world
    rid = approved_request(repo, program, media / "ProgramX")
    (media / "ProgramX" / "sub").mkdir(parents=True)
    (media / "ProgramX" / "sub" / "notes.txt").write_bytes(b"in the way")  # a conflict

    first = runner.run(rid)
    assert first.state == S.FAILED
    assert (media / "ProgramX" / "video.mxf").exists()

    repo.transition(rid, S.APPROVED, actor="user")  # the Retry button
    second = runner.run(rid, overwrite=frozenset({"sub/notes.txt"}))  # user agreed to replace it
    assert second.state == S.VERIFIED
    assert (media / "ProgramX" / "sub" / "notes.txt").read_bytes() == b"notes"
    assert [j["attempt"] for j in repo.list_jobs(rid)] == [1, 2]
    by_rel = {f.rel: f.outcome.value for f in second.tree.files}  # type: ignore[union-attr]
    assert by_rel["video.mxf"] == "SKIPPED_TRUSTED"  # verified by job 1: not read again


def test_crash_recovery_resumes_without_recopying(
    world, repo: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    storage, media, program, config, runner = world
    dst = media / "ProgramX"
    rid = approved_request(repo, program, dst)

    class Crash(BaseException):
        pass

    def die_on_video(tmp: Path) -> None:  # files go in name order: notes.txt is done first
        if tmp.name.startswith("video"):
            raise Crash

    real = mod.copy_tree
    with monkeypatch.context() as patch:
        patch.setattr(
            mod,
            "copy_tree",
            lambda plan, options, control, **kw: real(
                plan, options, control, hooks=Hooks(after_write=die_on_video), **kw
            ),
        )
        with pytest.raises(Crash):
            runner.run(rid)

    assert state(repo, rid) == "COPYING"  # the process "died" mid-copy
    assert repo.recover_interrupted() == [rid]
    assert state(repo, rid) == "APPROVED"

    outcome = runner.run(rid)
    assert outcome.state == S.VERIFIED
    assert (dst / "sub" / "notes.txt").read_bytes() == b"notes"
    assert not [p for p in dst.rglob("*") if p.name.endswith(".cspartial")]
    by_rel = {f.rel: f.outcome.value for f in outcome.tree.files}  # type: ignore[union-attr]
    assert by_rel["sub/notes.txt"] == "SKIPPED_TRUSTED"  # finished before the crash: not redone
    assert by_rel["video.mxf"] == "COPIED"


@pytest.mark.parametrize(
    ("src_kind", "dst_kind", "expect"),
    [
        ("outside", "ok", "not inside any configured folder"),
        ("ok", "outside", "outside the allowed root"),
        ("traversal", "ok", "not inside any configured folder"),
        ("ok", "root_ads", "Alternate data streams"),
    ],
)
def test_paths_outside_configured_roots_are_refused(  # type: ignore[no-untyped-def]
    world, repo: Repository, tmp_path: Path, src_kind: str, dst_kind: str, expect: str
) -> None:
    storage, media, program, config, runner = world
    other = tmp_path / "other"
    (other).mkdir()
    (other / "secret.txt").write_bytes(b"x")
    src = {"outside": other, "ok": program, "traversal": storage / ".." / "other"}[src_kind]
    dst = {"ok": media / "X", "outside": other / "X", "root_ads": Path(str(media) + r"\x:hidden")}[
        dst_kind
    ]
    rid = approved_request(repo, src, dst)

    outcome = runner.run(rid)

    assert outcome.state == S.FAILED
    assert expect in (outcome.error or "")
    assert not (other / "X").exists()


def test_source_and_destination_must_not_overlap(world, repo: Repository, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    storage, media, program, config, runner = world
    # The settings now refuse this layout outright; build it unvalidated to prove the job
    # still refuses on its own (defence in depth).
    dept = Department(name="Media Team", sender_emails=["a@b.org"], destination_root=str(tmp_path))
    cfg = AppConfig.model_construct(source_roots=[str(tmp_path)], departments=[dept])
    rid = approved_request(repo, program, program / "copy_of_itself")
    outcome = CopyJobRunner(repo, cfg).run(rid)
    assert outcome.state == S.FAILED
    assert "overlap" in (outcome.error or "")


def test_unknown_department_fails(world, repo: Repository) -> None:  # type: ignore[no-untyped-def]
    storage, media, program, config, _ = world
    rid = approved_request(repo, program, media / "X")
    empty = CopyJobRunner(repo, AppConfig(source_roots=[str(storage)]))
    outcome = empty.run(rid)
    assert outcome.state == S.FAILED
    assert "not in the settings" in (outcome.error or "")


def test_only_approved_requests_can_run(world, repo: Repository) -> None:  # type: ignore[no-untyped-def]
    storage, media, program, config, runner = world
    email_id = repo.add_email(
        gmail_message_id="x", gmail_thread_id="t", sender_email="anita@dept.org",
        received_at="2026-11-03T08:00:00+00:00", auth_status="verified", department="Media Team",
    )  # fmt: skip
    assert email_id is not None
    rid = repo.add_request(email_id, 1, {})
    repo.transition(rid, S.READY, actor="system", source_path=str(program))
    with pytest.raises(InvalidTransitionError):
        runner.run(rid)  # READY, not APPROVED: nobody confirmed the copy
    assert not (media / "X").exists()


def test_cancel_before_start_changes_nothing(world, repo: Repository) -> None:  # type: ignore[no-untyped-def]
    storage, media, program, config, runner = world
    rid = approved_request(repo, program, media / "X")
    control = Control()
    control.cancel()
    outcome = runner.run(rid, control)
    assert not outcome.started
    assert state(repo, rid) == "APPROVED"


def test_cancel_during_copy_marks_failed_for_retry(world, repo: Repository) -> None:  # type: ignore[no-untyped-def]
    storage, media, program, config, runner = world
    rid = approved_request(repo, program, media / "X")
    control = Control()
    outcome = runner.run(
        rid, control, progress=lambda p: control.cancel() if p.stage == "copy" else None
    )
    assert outcome.state == S.FAILED
    assert outcome.error == "Cancelled"
    repo.transition(rid, S.APPROVED, actor="user")  # can be retried
    assert runner.run(rid).state == S.VERIFIED


def test_options_follow_config() -> None:
    cfg = AppConfig(copy_options={"hash_algorithm": "blake3", "chunk_mb": 16, "max_attempts": 5})
    opts = options_from_config(cfg)
    assert (opts.algorithm, opts.chunk_bytes, opts.max_attempts) == ("blake3", 16 * MIB, 5)


# ------------------------------------------------------------------ queue
def test_queue_runs_jobs_and_forgets_them(world, repo: Repository) -> None:  # type: ignore[no-untyped-def]
    storage, media, program, config, runner = world
    ids = [approved_request(repo, program, media / f"X{i}", msg=f"m{i}") for i in range(3)]
    queue = CopyQueue(runner, concurrency=2)
    handles = [queue.submit(i) for i in ids]
    outcomes = [h.future.result(timeout=30) for h in handles]
    queue.shutdown()
    assert [o.state for o in outcomes] == [S.VERIFIED] * 3
    assert queue.active() == []


def test_queue_rejects_duplicate_submission(world, repo: Repository) -> None:  # type: ignore[no-untyped-def]
    storage, media, program, config, runner = world
    rid = approved_request(repo, program, media / "X")
    gate = threading.Event()
    queue = CopyQueue(runner, concurrency=1)
    first = queue.submit(rid, progress=lambda _p: gate.wait(5))
    with pytest.raises(ValueError, match="already queued"):
        queue.submit(rid)
    queue.pause(rid)
    queue.resume(rid)
    gate.set()
    assert first.future.result(timeout=30).state == S.VERIFIED
    queue.shutdown()


def test_queue_limits_concurrency(world, repo: Repository, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    storage, media, program, config, runner = world
    ids = [approved_request(repo, program, media / f"X{i}", msg=f"m{i}") for i in range(4)]
    running = {"now": 0, "max": 0}
    lock = threading.Lock()
    original = runner.run

    def tracked(request_id: int, control: Control | None = None, **kw) -> JobOutcome:  # type: ignore[no-untyped-def]
        with lock:
            running["now"] += 1
            running["max"] = max(running["max"], running["now"])
        try:
            time.sleep(0.2)
            return original(request_id, control, **kw)
        finally:
            with lock:
                running["now"] -= 1

    monkeypatch.setattr(runner, "run", tracked)
    queue = CopyQueue(runner, concurrency=2)
    for rid in ids:
        queue.submit(rid)
    queue.shutdown()
    assert running["max"] == 2


def test_queue_cancel_running_job(world, repo: Repository) -> None:  # type: ignore[no-untyped-def]
    storage, media, program, config, runner = world
    rid = approved_request(repo, program, media / "X")
    queue = CopyQueue(runner, concurrency=1)
    started = threading.Event()
    handle = queue.submit(rid, progress=lambda _p: started.set())
    assert started.wait(10)
    queue.cancel(rid)
    outcome = handle.future.result(timeout=30)
    queue.shutdown()
    assert outcome.state in {S.FAILED, S.VERIFIED}  # tiny files may finish before the cancel lands
    queue.cancel(999)  # unknown ids are ignored


def test_queue_shutdown_with_cancel(world, repo: Repository) -> None:  # type: ignore[no-untyped-def]
    storage, media, program, config, runner = world
    rid = approved_request(repo, program, media / "X")
    queue = CopyQueue(runner, concurrency=1)
    handle = queue.submit(rid, progress=lambda _p: time.sleep(0.05))
    queue.shutdown(cancel=True)
    assert handle.future.done()


# ------------------------------------------------------------------ migration v1 -> v2
def test_upgrade_from_v1_database_keeps_data(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    for statement in __import__(
        "contentshare.storage.db", fromlist=["_split_script"]
    )._split_script(MIGRATIONS[0]):
        conn.execute(statement)
    conn.execute("PRAGMA user_version = 1")
    conn.execute(
        "INSERT INTO emails (gmail_message_id, gmail_thread_id, sender_email, received_at, "
        "auth_status, created_at) VALUES ('old', 't', 'a@b.org', 'x', 'verified', 'x')"
    )
    conn.commit()
    conn.close()

    db = Database(path)  # opens and migrates
    assert db.migrate() == len(MIGRATIONS)
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 1
        columns = {r["name"] for r in c.execute("PRAGMA table_info(job_files)")}
    assert "mtime_ns" in columns

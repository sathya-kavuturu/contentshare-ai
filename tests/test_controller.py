"""The logic behind the screens: review, approve, copy, reply, history. No Qt involved."""

from __future__ import annotations

import csv
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from contentshare.app.controller import (
    ActionError,
    Controller,
    Event,
    RequestView,
    csv_safe,
    describe_item,
)
from contentshare.config.store import load_config
from contentshare.copy_engine import format_duration
from contentshare.gmail.errors import AuthRequiredError
from contentshare.jobs.states import RequestState as S
from contentshare.storage import repository as repository_module
from contentshare.storage.db import Database
from fake_gmail import auth_header, make_message
from helpers import TODAY, MailWorld, build_mail_world

GOOD = "Sunrise Talk 12 Oct 2026"


class Harness:
    def __init__(self, world: MailWorld, tmp_path: Path) -> None:
        self.world = world
        self.events: list[Event] = []
        self.finished: dict[int, threading.Event] = {}
        self.replied: dict[int, threading.Event] = {}
        self.ctl = Controller(
            world.repo, world.index, world.config,
            gateway_factory=lambda: world.gateway,
            config_path=tmp_path / "config.yaml", today=lambda: TODAY,
        )  # fmt: skip
        self.ctl.subscribe(self._on_event)

    def _on_event(self, event: Event) -> None:
        self.events.append(event)
        if event.request_id is None:
            return
        if event.kind == "copy_finished":
            self.finished.setdefault(event.request_id, threading.Event()).set()
        if event.kind in ("reply_sent", "reply_problem"):
            self.replied.setdefault(event.request_id, threading.Event()).set()

    def wait_copy(self, request_id: int) -> None:
        assert self.finished.setdefault(request_id, threading.Event()).wait(30), "copy timed out"

    def wait_reply(self, request_id: int) -> None:
        assert self.replied.setdefault(request_id, threading.Event()).wait(30), "no reply event"

    def mail(self, **kwargs: object) -> None:
        self.world.gmail.add(make_message(**kwargs))  # type: ignore[arg-type]
        self.ctl.check_now()

    def ready(self) -> RequestView:
        self.mail(message_id="m1", subject=GOOD, body="Please share the English recording.\nThanks")
        views = self.ctl.views([S.READY])
        assert len(views) == 1, [(v.state, v.reason) for v in self.ctl.views()]
        return views[0]


@pytest.fixture
def h(tmp_path: Path, db: Database) -> Iterator[Harness]:
    harness = Harness(build_mail_world(tmp_path, db), tmp_path)
    yield harness
    harness.ctl.shutdown()


# --------------------------------------------------------------------------- reading
def test_check_now_produces_views_with_reasons(h: Harness) -> None:
    v = h.ready()
    assert v.state == "READY"
    assert v.department == "Media Team"
    assert "SunriseTalk_EN" in (v.source_path or "")
    assert v.requested.startswith("Sunrise Talk")
    assert "12 Oct 2026" in v.requested
    assert v.verified_sender
    assert v.size_text.endswith("KB") or v.size_text.endswith("B")
    assert h.ctl.last_check() is not None
    assert h.ctl.counts()["READY"] == 1


def test_describe_item_handles_ranges_and_parts() -> None:
    text = describe_item(
        {"program": "Retreat", "dates": [["2026-10-12", "2026-10-14"]], "part": ["day", 2]}
    )
    assert text == "Retreat · 12 Oct 2026 – 14 Oct 2026 · day 2"  # noqa: RUF001
    assert describe_item({}) == "(program not recognised)"


def test_unreadable_request_json_does_not_break_the_list(h: Harness) -> None:
    v = h.ready()
    with h.world.repo.db.transaction() as conn:
        conn.execute("UPDATE requests SET query_json = 'not json' WHERE id = ?", (v.id,))
    view = h.ctl.view(v.id)
    assert view.candidates == () and view.reason == ""


# --------------------------------------------------------------------------- approving
def test_approve_needs_the_confirm_tick(h: Harness) -> None:
    v = h.ready()
    with pytest.raises(ActionError, match="Confirm copy"):
        h.ctl.approve(v.id, confirmed=False)
    assert h.ctl.view(v.id).state == "READY"


def test_approve_copies_verifies_and_leaves_reply_for_the_user(h: Harness) -> None:
    v = h.ready()
    result = h.ctl.approve(v.id, confirmed=True)
    assert result.started
    h.wait_copy(v.id)
    after = h.ctl.view(v.id)
    assert after.state == "VERIFIED"  # auto_reply is off: nothing was sent
    assert h.world.gmail.sent == []
    copied = Path(v.destination_path or "")
    assert (copied / "video.mxf").stat().st_size == 2000
    assert (copied / "audio.wav").exists()


def test_auto_reply_sends_after_a_verified_copy(h: Harness) -> None:
    v = h.ready()
    h.ctl.set_auto_reply(v.id, True)
    h.ctl.approve(v.id, confirmed=True)
    h.wait_copy(v.id)
    h.wait_reply(v.id)
    assert h.ctl.view(v.id).state == "REPLIED"
    assert len(h.world.gmail.sent) == 1


def test_manual_reply_flow_with_edit(h: Harness) -> None:
    v = h.ready()
    h.ctl.approve(v.id, confirmed=True)
    h.wait_copy(v.id)
    draft = h.ctl.reply_draft(v.id)
    assert draft.to_addr == "anita@dept.org"
    result = h.ctl.send_reply(draft.with_body(draft.body + "\nP.S. thanks"))
    assert result.status == "SENT"
    assert h.ctl.view(v.id).state == "REPLIED"
    assert "P.S. thanks" in h.ctl.replies_for(v.id)[0]["body"]
    assert any(e.kind == "reply_sent" for e in h.events)


def test_reply_draft_refused_before_verification(h: Harness) -> None:
    v = h.ready()
    with pytest.raises(ActionError, match="not been verified"):
        h.ctl.reply_draft(v.id)


def test_double_approve_is_refused(h: Harness) -> None:
    v = h.ready()
    h.ctl.approve(v.id, confirmed=True)
    with pytest.raises(ActionError, match="Only a Ready"):
        h.ctl.approve(v.id, confirmed=True)
    h.wait_copy(v.id)


def test_unverified_sender_needs_acknowledgement(h: Harness) -> None:
    h.mail(
        message_id="m2", from_="Boss <editor@dept.org>", subject=GOOD,
        auth=auth_header("dept.org", dmarc="fail", spf="fail", dkim="fail"),
    )  # fmt: skip
    review = h.ctl.views([S.NEEDS_REVIEW])
    assert len(review) == 1 and not review[0].verified_sender
    assert review[0].candidates
    h.ctl.choose(review[0].id, review[0].candidates[0].item_id or 0)
    assert h.ctl.view(review[0].id).state == "READY"
    with pytest.raises(ActionError, match="could not be verified"):
        h.ctl.approve(review[0].id, confirmed=True)
    assert h.ctl.approve(review[0].id, confirmed=True, acknowledge_unverified=True).started
    h.wait_copy(review[0].id)
    assert h.ctl.view(review[0].id).state == "VERIFIED"


def test_source_vanished_is_caught_before_copy(h: Harness) -> None:
    v = h.ready()
    import shutil

    shutil.rmtree(v.source_path or "")
    with pytest.raises(ActionError, match="no longer on the storage server"):
        h.ctl.approve(v.id, confirmed=True)
    assert h.ctl.view(v.id).state == "READY"


def test_content_changed_since_index_is_remeasured(h: Harness) -> None:
    v = h.ready()
    (Path(v.source_path or "") / "extra.bin").write_bytes(b"z" * 50)
    result = h.ctl.approve(v.id, confirmed=True)
    assert "changed since it was indexed" in result.note
    h.wait_copy(v.id)
    assert (Path(v.destination_path or "") / "extra.bin").exists()
    assert h.ctl.view(v.id).file_count == v.file_count + 1  # type: ignore[operator]


def test_path_outside_roots_is_refused(h: Harness, tmp_path: Path) -> None:
    v = h.ready()
    with h.world.repo.db.transaction() as conn:
        conn.execute(
            "UPDATE requests SET source_path = ? WHERE id = ?", (str(tmp_path / "elsewhere"), v.id)
        )
    with pytest.raises(ActionError, match="Path not allowed"):
        h.ctl.approve(v.id, confirmed=True)


def test_source_and_destination_overlap_is_refused(h: Harness) -> None:
    v = h.ready()
    with h.world.repo.db.transaction() as conn:
        conn.execute(
            "UPDATE requests SET destination_path = ? WHERE id = ?",
            (str(Path(h.world.media)), v.id),
        )
    # point the source inside the destination root's tree via config: make roots overlap
    cfg = h.world.config.model_copy(update={"source_roots": [str(h.world.media.parent)]})
    h.ctl.apply_config(cfg, persist=False)
    with h.world.repo.db.transaction() as conn:
        conn.execute("UPDATE requests SET source_path = ? WHERE id = ?", (str(h.world.media), v.id))
    with pytest.raises(ActionError, match="overlap"):
        h.ctl.approve(v.id, confirmed=True)


# --------------------------------------------------------------------------- dry run
def test_dry_run_reports_and_changes_nothing(h: Harness) -> None:
    v = h.ready()
    h.ctl.set_dry_run(True)
    assert h.ctl.dry_run
    result = h.ctl.approve(v.id, confirmed=True)
    assert result.dry_run and not result.started
    assert result.files == 3 and result.total_bytes == 2510
    assert result.problems == ()
    assert h.ctl.view(v.id).state == "READY"
    assert not Path(v.destination_path or "").exists()
    assert h.world.gmail.sent == []
    with pytest.raises(ActionError, match="Dry-run"):
        h.ctl.send_reply(None)  # type: ignore[arg-type]
    assert h.ctl.resume_approved() == []


def test_dry_run_reports_problems(h: Harness) -> None:
    v = h.ready()
    h.ctl.set_dry_run(True)
    Path(h.world.media, "Sunrise").mkdir(exist_ok=True)
    dest = Path(v.destination_path or "")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.mkdir(exist_ok=True)
    (dest / "video.mxf").write_bytes(b"different")
    result = h.ctl.approve(v.id, confirmed=True)
    assert result.dry_run  # a clash is shown, never overwritten
    assert h.ctl.view(v.id).state == "READY"
    assert (dest / "video.mxf").read_bytes() == b"different"


def test_dry_run_is_remembered_in_settings_file(h: Harness, tmp_path: Path) -> None:
    h.ctl.set_dry_run(True)
    assert load_config(tmp_path / "config.yaml").dry_run is True


# --------------------------------------------------------------------------- review actions
def test_manual_search_and_choose(h: Harness) -> None:
    h.mail(message_id="m3", subject="Quantum Lecture", body="need it")
    review = h.ctl.views([S.NEEDS_REVIEW])
    assert len(review) == 1 and review[0].candidates == ()
    found = h.ctl.search(review[0].id, "Annual Day 2025")
    assert found and "AnnualDay" in found[0].path
    assert h.ctl.search(review[0].id, "   ") == []
    h.ctl.choose(review[0].id, found[0].item_id or 0)
    chosen = h.ctl.view(review[0].id)
    assert chosen.state == "READY"
    assert chosen.source_path and chosen.source_path.endswith("AnnualDay_2025-12-25")
    assert (chosen.destination_path or "").startswith(str(h.world.media))
    assert chosen.file_count == 3
    assert any(e["event"] == "state_changed" for e in h.ctl.audit(chosen.id))


def test_manual_search_ignores_the_emails_date_and_language(h: Harness) -> None:
    """Regression: the request asked for 12 Oct 2026 English; a user who now types a different
    program must still find it (the old date used to push it out of the results)."""
    v = h.ready()
    found = h.ctl.search(v.id, "Annual Day 2025")
    assert any("AnnualDay_2025-12-25" in c.path for c in found)
    dated = h.ctl.search(v.id, "Sunrise Talk 19 Oct 2026")
    assert "10-19" in dated[0].path  # typing a date still narrows the results


def test_change_choice_while_ready(h: Harness) -> None:
    v = h.ready()
    other = h.ctl.search(v.id, "Sunrise Talk 19 Oct")
    pick = next(c for c in other if "10-19" in c.path)
    h.ctl.choose(v.id, pick.item_id or 0)
    after = h.ctl.view(v.id)
    assert after.state == "READY" and "10-19" in (after.source_path or "")
    assert any(e["event"] == "request_edited" for e in h.ctl.audit(v.id))


def test_choose_rejects_vanished_or_unknown_items(h: Harness) -> None:
    v = h.ready()
    with pytest.raises(ActionError, match="not in the index"):
        h.ctl.choose(v.id, 999_999)
    candidate = h.ctl.search(v.id, "Annual Day")[0]
    import shutil

    shutil.rmtree(candidate.path)
    with pytest.raises(ActionError, match="not on the storage server"):
        h.ctl.choose(v.id, candidate.item_id or 0)


def test_choose_for_unknown_department_is_refused(h: Harness) -> None:
    v = h.ready()
    found = h.ctl.search(v.id, "Annual Day")
    # the department was removed from the settings after the request arrived
    cfg = h.ctl.config.model_copy(update={"departments": h.ctl.config.departments[1:]})
    h.ctl.apply_config(cfg, persist=False)
    with pytest.raises(ActionError, match="not a department"):
        h.ctl.choose(v.id, found[0].item_id or 0)
    with pytest.raises(ActionError, match="not a department"):
        h.ctl.approve(v.id, confirmed=True)


def test_set_destination_stays_inside_department_folder(h: Harness) -> None:
    v = h.ready()
    ok = Path(h.world.media, "Custom", "Folder")
    h.ctl.set_destination(v.id, str(ok))
    assert h.ctl.view(v.id).destination_path == str(ok)
    with pytest.raises(ActionError, match="must be inside"):
        h.ctl.set_destination(v.id, str(h.world.media.parent / "elsewhere"))
    with pytest.raises(ActionError, match="must be inside"):
        h.ctl.set_destination(v.id, str(h.world.media / ".." / "x"))


def test_skip_and_reject_and_terminal_states_are_final(h: Harness) -> None:
    v = h.ready()
    h.ctl.skip(v.id)
    assert h.ctl.view(v.id).state == "SKIPPED"
    with pytest.raises(ActionError):
        h.ctl.skip(v.id)
    with pytest.raises(ActionError):
        h.ctl.set_auto_reply(v.id, True)
    with pytest.raises(ActionError, match="no longer be changed"):
        h.ctl.choose(v.id, 1)
    with pytest.raises(ActionError, match="before approval"):
        h.ctl.set_destination(v.id, str(h.world.media))


def test_reject_then_send_rejection_reply(h: Harness) -> None:
    v = h.ready()
    h.ctl.reject(v.id, "Not available")
    assert h.ctl.view(v.id).state == "REJECTED"
    draft = h.ctl.rejection_draft(v.id, "Sorry, this one is not available yet.")
    assert h.ctl.send_reply(draft).status == "SENT"
    assert h.ctl.view(v.id).state == "REJECTED"
    with pytest.raises(ActionError):
        h.ctl.rejection_draft(h.ctl.views()[0].id + 99)


def test_unknown_request_is_an_action_error(h: Harness) -> None:
    with pytest.raises(ActionError, match="no longer exists"):
        h.ctl.view(12345)
    with pytest.raises(ActionError, match="no longer exists"):
        h.ctl.approve(12345, confirmed=True)


# --------------------------------------------------------------------------- failures and restarts
def test_failed_copy_can_be_retried_and_never_offers_a_reply(h: Harness) -> None:
    v = h.ready()
    dest = Path(v.destination_path or "")
    dest.mkdir(parents=True)
    (dest / "video.mxf").write_bytes(b"someone else's file")  # conflict: never overwritten
    h.ctl.approve(v.id, confirmed=True)
    h.wait_copy(v.id)
    failed = h.ctl.view(v.id)
    assert failed.state == "FAILED" and failed.failure_reason
    with pytest.raises(ActionError, match="not been verified"):
        h.ctl.reply_draft(v.id)
    (dest / "video.mxf").unlink()
    h.finished.clear()
    h.ctl.retry(v.id)
    h.wait_copy(v.id)
    assert h.ctl.view(v.id).state == "VERIFIED"
    with pytest.raises(ActionError, match="Only a failed"):
        h.ctl.retry(v.id)


def test_resume_approved_after_restart(h: Harness) -> None:
    v = h.ready()
    h.world.repo.transition(v.id, S.APPROVED, actor="user")
    h.world.repo.transition(v.id, S.COPYING, actor="system")  # the app died here
    assert h.ctl.resume_approved() == [v.id]
    h.wait_copy(v.id)
    assert h.ctl.view(v.id).state == "VERIFIED"


def test_pause_resume_cancel_active_copy(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    v = h.ready()
    gate = threading.Event()
    real_run = h.ctl.queue._runner.run

    def slow(request_id: int, control: object, **kw: object) -> object:
        gate.wait(10)
        return real_run(request_id, control, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(h.ctl.queue._runner, "run", slow)
    h.ctl.approve(v.id, confirmed=True)
    active = h.ctl.active_copies()
    assert [a.request_id for a in active] == [v.id] and not active[0].paused
    assert h.ctl.busy()
    h.ctl.pause(v.id)
    assert h.ctl.active_copies()[0].paused
    h.ctl.resume(v.id)
    assert not h.ctl.active_copies()[0].paused
    with pytest.raises(ActionError, match="running copies"):
        h.ctl.apply_config(h.ctl.config, persist=False)
    h.ctl.cancel(v.id)
    gate.set()
    h.wait_copy(v.id)
    assert h.ctl.view(v.id).state == "APPROVED"  # cancelled before it began: still approved
    assert not h.ctl.busy()


# --------------------------------------------------------------------------- gmail, index, settings
def test_check_now_without_sign_in_reports_it(h: Harness) -> None:
    def refuse() -> None:
        raise AuthRequiredError("sign in")

    ctl = Controller(
        h.world.repo, h.world.index, h.world.config,
        gateway_factory=refuse, today=lambda: TODAY,
    )  # fmt: skip
    assert ctl.check_now().needs_sign_in
    ctl.shutdown()


def test_gmail_connection_helpers(h: Harness, tmp_path: Path) -> None:
    assert not h.ctl.gmail_connected()
    with pytest.raises(ActionError, match="not available"):
        h.ctl.connect_gmail()
    with pytest.raises(ActionError, match="not available"):
        h.ctl.disconnect_gmail()

    class FakeAuth:
        connected = False

        def __init__(self) -> None:
            self.scopes: list[str] = []

        def is_connected(self) -> bool:
            return self.connected

        def connect(self, path: str, scopes: list[str]) -> None:
            self.connected, self.scopes = True, scopes

        def disconnect(self) -> object:
            self.connected = False
            return type("O", (), {"revoked": True, "error": None})()

    auth = FakeAuth()
    h.ctl.auth = auth  # type: ignore[assignment]
    with pytest.raises(ActionError, match="client_secret"):
        h.ctl.connect_gmail()
    secret = tmp_path / "client_secret.json"
    secret.write_text("{}")
    h.ctl.apply_config(
        h.ctl.config.model_copy(
            update={
                "gmail": h.ctl.config.gmail.model_copy(update={"client_secret_path": str(secret)})
            }
        ),
        persist=False,
    )
    h.ctl.connect_gmail()
    assert h.ctl.gmail_connected() and auth.scopes
    assert "revoked" in h.ctl.disconnect_gmail()


def test_rescan_index_and_summary(h: Harness) -> None:
    before = h.ctl.index_summary()
    assert before["items"] > 0 and before["last_scan"]
    Path(h.world.storage, "2026", "NewProgram_2026-11-01").mkdir()
    Path(h.world.storage, "2026", "NewProgram_2026-11-01", "a.mp4").write_bytes(b"x")
    reports = h.ctl.rescan_index()
    assert reports[0].status == "complete"
    assert h.ctl.index_summary()["items"] > before["items"]


def test_apply_config_persists_and_forgets_removed_roots(h: Harness, tmp_path: Path) -> None:
    cfg = h.ctl.config.model_copy(update={"source_roots": []})
    h.ctl.apply_config(cfg)
    assert load_config(tmp_path / "config.yaml").source_roots == []
    assert h.ctl.index.stats()["items"] == 0


def test_recover_replies_noop_and_listener_errors_are_contained(h: Harness) -> None:
    assert h.ctl.recover_replies() == []

    def broken(_: Event) -> None:
        raise RuntimeError("boom")

    h.ctl.subscribe(broken)
    h.ready()  # events are emitted despite the broken listener
    assert h.events


# --------------------------------------------------------------------------- history / csv
def test_history_export_defuses_spreadsheet_formulas(h: Harness, tmp_path: Path) -> None:
    h.mail(
        message_id="m5", from_='"=cmd|calc" <anita@dept.org>',
        subject='=HYPERLINK("http://evil","x")', body="Annual Day 2025",
    )  # fmt: skip
    out = tmp_path / "history.csv"
    n = h.ctl.export_history(out)
    assert n == len(h.ctl.views())
    with out.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows
    for row in rows:
        for column in ("from", "subject", "requested"):
            assert not row[column].startswith(("=", "+", "-", "@"))
    assert any(r["subject"].startswith("'=HYPERLINK") for r in rows)


def test_csv_safe() -> None:
    assert csv_safe("=1+1") == "'=1+1"
    assert csv_safe("-5") == "'-5"
    assert csv_safe(None) == ""
    assert csv_safe("plain") == "plain"


def test_open_gmail_thread_ids_are_exposed(h: Harness) -> None:
    v = h.ready()
    assert v.gmail_thread_id and v.gmail_message_id


# --------------------------------------------------------------------------- copy time
def test_history_shows_how_long_the_copy_took(h: Harness, tmp_path: Path) -> None:
    v = h.ready()
    assert v.copy_seconds is None and v.copy_time_text == "" and v.copy_speed_text == ""
    h.ctl.approve(v.id, confirmed=True)
    h.wait_copy(v.id)
    after = h.ctl.view(v.id)
    assert after.copy_seconds is not None and 0 <= after.copy_seconds < 30
    assert after.copy_time_text
    out = tmp_path / "history.csv"
    h.ctl.export_history(out)
    with out.open(encoding="utf-8-sig", newline="") as fh:
        rows = {int(r["request"]): r for r in csv.DictReader(fh)}
    assert float(rows[v.id]["copy_seconds"]) == pytest.approx(after.copy_seconds, abs=0.06)


def test_copy_time_is_that_of_the_last_finished_job(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    v = h.ready()
    repo = h.world.repo
    stamps = iter(
        [
            "2026-11-04T10:00:00.000+00:00", "2026-11-04T10:02:05.500+00:00",  # 125.5 s
            "2026-11-04T11:00:00.000+00:00",  # a job that never finished (crash)
            "2026-11-04T12:00:00.250+00:00", "2026-11-04T12:00:03.000+00:00",  # 2.75 s
        ]
    )  # fmt: skip
    monkeypatch.setattr(repository_module, "_now_precise", lambda: next(stamps))
    first, _ = repo.create_job(v.id)
    repo.finish_job(first, "FAILED", "network")
    assert h.ctl.view(v.id).copy_seconds == pytest.approx(125.5, abs=0.01)
    repo.create_job(v.id)  # started, never finished: ignored
    assert h.ctl.view(v.id).copy_seconds == pytest.approx(125.5, abs=0.01)
    third, _ = repo.create_job(v.id)
    repo.finish_job(third, "DONE")
    assert h.ctl.view(v.id).copy_seconds == pytest.approx(2.75, abs=0.01)


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0.2, "< 1 s"),
        (4.24, "4.2 s"),
        (38.4, "38 s"),
        (125, "2 min 05 s"),
        (3599.4, "59 min 59 s"),
        (3725, "1 h 02 min"),
    ],
)
def test_format_duration(seconds: float, text: str) -> None:
    assert format_duration(seconds) == text


def test_copy_speed_text_is_the_average_over_the_whole_job(h: Harness) -> None:
    v = h.ready()
    view = RequestView(**{**v.__dict__, "total_bytes": 50 * 1024 * 1024, "copy_seconds": 10.0})
    assert view.copy_speed_text == "5.0 MB/s"
    assert RequestView(**{**v.__dict__, "copy_seconds": 0.0}).copy_speed_text == ""


# --------------------------------------------------------------------------- who approved
def test_history_says_who_approved_the_request(h: Harness, tmp_path: Path) -> None:
    h.ctl.create_user("asha", "pw")
    v = h.ready()
    assert v.approved_by == ""
    h.ctl.approve(v.id, confirmed=True)
    h.wait_copy(v.id)
    assert h.ctl.view(v.id).approved_by == "asha"
    out = tmp_path / "history.csv"
    h.ctl.export_history(out)
    with out.open(encoding="utf-8-sig", newline="") as fh:
        rows = {int(r["request"]): r for r in csv.DictReader(fh)}
    assert rows[v.id]["approved_by"] == "asha"
    who = {e["actor"] for e in h.ctl.audit(v.id) if e["event"] == "state_changed"}
    assert "asha" in who and "system" in who


def test_a_dry_run_check_is_also_attributed(h: Harness) -> None:
    h.ctl.create_user("ravi", "x")
    h.ctl.apply_config(h.ctl.config.model_copy(update={"dry_run": True}))
    v = h.ready()
    h.ctl.approve(v.id, confirmed=True)
    assert h.ctl.view(v.id).approved_by == "ravi"


# --------------------------------------------------------------------------- users and passwords
def test_users_sign_in_with_any_password_and_are_told_apart(h: Harness) -> None:
    from contentshare.app.controller import ActionError, clean_user_name

    assert h.ctl.user_name == "user" and not h.ctl.signed_in  # nobody yet
    assert clean_user_name("  Asha " + chr(9) + "  Rao" + chr(10)) == "Asha Rao"
    assert clean_user_name("x" * 100) == "x" * 40
    assert h.ctl.create_user("  Meera  K ", "a") == "Meera K"  # a one-letter password is fine
    assert h.ctl.signed_in and h.ctl.user_name == "Meera K"
    h.ctl.sign_out()
    assert not h.ctl.signed_in
    with pytest.raises(ActionError):
        h.ctl.sign_in("Meera K", "wrong")
    with pytest.raises(ActionError):
        h.ctl.sign_in("Nobody", "a")
    assert h.ctl.sign_in("meera k", "a") == "Meera K"  # capitals do not matter in the name
    for name, password in (("   ", "x"), ("Ravi", "")):
        with pytest.raises(ActionError):
            h.ctl.create_user(name, password)
    with pytest.raises(ActionError):
        h.ctl.create_user("MEERA K", "other")  # the name is taken
    assert h.ctl.user_names() == ["Meera K"]
    assert h.ctl.last_user() == "Meera K"
    assert load_config(h.ctl._config_path).user_name == "Meera K"  # type: ignore[arg-type]


def test_passwords_are_stored_as_salted_hashes_never_as_typed(h: Harness) -> None:
    h.ctl.create_user("Asha", "secret-word")
    h.ctl.create_user("Ravi", "secret-word")
    rows = [h.ctl.repo.get_user(n) for n in ("Asha", "Ravi")]
    assert all(r and "secret-word" not in str(r) for r in rows)
    assert rows[0]["pw_hash"] != rows[1]["pw_hash"]  # a different salt each
    trail = str(h.ctl.repo.audit_entries())
    assert "secret-word" not in trail


def test_nothing_is_done_before_signing_in_when_the_app_requires_it(h: Harness) -> None:
    from contentshare.app.controller import ActionError

    v = h.ready()
    h.ctl.login_required = True
    with pytest.raises(ActionError, match="Sign in"):
        h.ctl.approve(v.id, confirmed=True)
    with pytest.raises(ActionError, match="Sign in"):
        h.ctl.skip(v.id)
    h.ctl.create_user("Asha", "pw")
    h.ctl.approve(v.id, confirmed=True)  # signed in: allowed
    h.wait_copy(v.id)
    assert h.ctl.view(v.id).approved_by == "Asha"


# --------------------------------------------------------------------------- copy by itself
def _turn_on_auto_copy(h: Harness, on: bool = True) -> None:
    h.ctl.apply_config(h.ctl.config.model_copy(update={"auto_copy_certain": on}), persist=False)


def test_a_certain_ready_request_is_copied_and_answered_without_approval(h: Harness) -> None:
    v = h.ready()
    h.world.repo.update_request(v.id, actor="test", reason="exact", confidence=100.0)
    assert h.ctl.auto_copy_certain() == []  # the setting is off: it waits for a person
    assert h.ctl.view(v.id).state == "READY"
    _turn_on_auto_copy(h)
    assert h.ctl.auto_copy_certain() == [v.id]
    h.wait_copy(v.id)
    h.wait_reply(v.id)
    assert h.ctl.view(v.id).state == "REPLIED" and len(h.world.gmail.sent) == 1
    assert h.ctl.view(v.id).approved_by == "automatic"


def test_a_request_below_100_percent_still_waits_for_a_person(h: Harness) -> None:
    v = h.ready()
    h.world.repo.update_request(v.id, actor="test", reason="close", confidence=97.0)
    _turn_on_auto_copy(h)
    assert h.ctl.auto_copy_certain() == []
    assert h.ctl.view(v.id).state == "READY"


def test_auto_copy_does_nothing_in_dry_run(h: Harness) -> None:
    v = h.ready()
    h.world.repo.update_request(v.id, actor="test", reason="exact", confidence=100.0)
    h.ctl.apply_config(
        h.ctl.config.model_copy(update={"auto_copy_certain": True, "dry_run": True}), persist=False
    )
    assert h.ctl.auto_copy_certain() == []
    assert h.ctl.view(v.id).state == "READY"

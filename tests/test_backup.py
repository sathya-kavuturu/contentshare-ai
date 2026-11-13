"""Backup requests: a department's own content goes into the archive (the other way round from a
normal request), never overwriting what is there without the person choosing."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError

from contentshare.app.controller import ActionError, Controller, Event
from contentshare.config.models import AppConfig
from contentshare.gmail.gateway import GmailGateway
from contentshare.index import ContentIndex
from contentshare.matching.backup import (
    asks_for_backup,
    path_hints,
    process_backup_requests,
)
from contentshare.storage.db import Database
from contentshare.storage.repository import Repository
from fake_gmail import FakeGmail, make_message
from helpers import TODAY, make_storage

NAME = "English AAA Training 10-11-2026"
BODY = (
    "Namaskaram,\nThe following content we kept in the *{folder}* folder:\n- " + NAME + "\n"
    "Could you please back it up?\nPranam,\nSathya"
)


@dataclass
class World:
    dept_server: Path  # the department's own server
    folder: Path  # the folder inside it the department named
    archive: Path
    config: AppConfig
    repo: Repository
    gmail: FakeGmail
    ctl: Controller
    events: list[Event]
    finished: dict[int, threading.Event]

    def mail(self, body: str | None = None, **kwargs: object) -> None:
        text = body if body is not None else BODY.format(folder=self.folder)
        self.gmail.add(make_message(subject="Backup please", body=text, **kwargs))  # type: ignore[arg-type]
        self.ctl.check_now()

    def wait_copy(self, request_id: int) -> None:
        done = self.finished.setdefault(request_id, threading.Event())
        assert done.wait(30), "copy timed out"


def make_config(tmp_path: Path, **extra: object) -> AppConfig:
    storage = make_storage(tmp_path / "Programs")
    media = tmp_path / "media_incoming"
    media.mkdir(exist_ok=True)
    dept_server = tmp_path / "dept_server"
    department = {
        "name": "Media Team",
        "sender_emails": ["anita@dept.org"],
        "destination_root": str(media),
        "backup_enabled": True,
        "backup_source": str(dept_server),
        "backup_archive": str(tmp_path / "archives" / "media"),
    }
    department.update(extra)  # type: ignore[arg-type]
    return AppConfig(
        source_roots=[str(storage)],
        departments=[department],
        backup={"archive_root": str(tmp_path / "archives" / "general")},
    )


@pytest.fixture
def w(tmp_path: Path, db: Database) -> Iterator[World]:
    config = make_config(tmp_path)
    folder = tmp_path / "dept_server" / "Live Stream to IIA"
    content = folder / NAME
    content.mkdir(parents=True)
    (content / "video.mxf").write_bytes(b"v" * 2000)
    (content / "audio.wav").write_bytes(b"a" * 500)
    repo = Repository(db)
    index = ContentIndex(db, config.index)
    gmail = FakeGmail()
    gateway = GmailGateway(gmail, sleep=lambda _s: None)
    events: list[Event] = []
    finished: dict[int, threading.Event] = {}

    def on_event(event: Event) -> None:
        events.append(event)
        if event.kind == "copy_finished" and event.request_id is not None:
            finished.setdefault(event.request_id, threading.Event()).set()

    ctl = Controller(
        repo, index, config, gateway_factory=lambda: gateway,
        config_path=tmp_path / "config.yaml", today=lambda: TODAY,
    )  # fmt: skip
    ctl.subscribe(on_event)
    yield World(
        tmp_path / "dept_server", folder, tmp_path / "archives" / "media", config, repo, gmail,
        ctl, events, finished,
    )  # fmt: skip
    ctl.shutdown()


# --------------------------------------------------------------------------- settings
def test_backups_on_need_a_source_and_an_archive(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="which folder"):
        make_config(tmp_path, backup_source="")
    base = make_config(tmp_path).model_dump()
    base["backup"]["archive_root"] = ""
    base["departments"][0]["backup_archive"] = ""
    with pytest.raises(ValidationError, match="no archive folder"):
        AppConfig.model_validate(base)


def test_backup_folders_must_be_apart(tmp_path: Path) -> None:
    server = str(tmp_path / "dept_server")
    with pytest.raises(ValidationError, match="inside each other"):
        make_config(tmp_path, backup_archive=str(Path(server) / "archive"))
    with pytest.raises(ValidationError, match="overlaps the folder"):
        make_config(tmp_path, backup_archive=str(tmp_path / "media_incoming" / "backups"))


def test_a_department_without_its_own_archive_uses_the_general_one(tmp_path: Path) -> None:
    config = make_config(tmp_path, backup_archive="")
    ctx = config.backup_context("Media Team")
    assert ctx.archive_root == str(tmp_path / "archives" / "general")
    assert config.backup_context(None).sources == ()  # no allowed folders for strangers
    assert not config.backup_context(None).usable


# --------------------------------------------------------------------------- reading the email
def test_the_wording_of_a_backup_request() -> None:
    assert asks_for_backup("", "Could you please back it up?")
    assert asks_for_backup("Backup request", "")
    assert asks_for_backup("", "please archive them")
    assert not asks_for_backup("", "please share it from the archives")
    assert not asks_for_backup("", "Send me the English recording")


def test_a_path_that_the_email_program_wrapped_is_read_whole() -> None:
    bs = chr(92)
    path = bs * 2 + "ip" + bs + "sampple_Share" + bs + "Live"
    hints = path_hints("", f"kept in the *{path}\nStream to IIA* folder:\n- x")
    assert path + " Stream to IIA" in hints


# --------------------------------------------------------------------------- the request
def test_a_backup_is_read_from_the_department_and_goes_to_the_archive(w: World) -> None:
    w.mail()
    (v,) = w.ctl.views()
    assert v.kind == "backup" and v.state == "READY"
    assert v.source_path == str(w.folder / NAME)
    assert v.destination_path == str(w.archive / NAME)
    assert v.file_count == 2 and v.total_bytes == 2500
    assert not v.archive_exists


def test_approving_a_backup_copies_into_the_archive_and_leaves_the_source(w: World) -> None:
    w.mail()
    (v,) = w.ctl.views()
    assert w.ctl.approve(v.id, confirmed=True).started
    w.wait_copy(v.id)
    assert w.ctl.view(v.id).state == "VERIFIED"
    assert (w.archive / NAME / "video.mxf").stat().st_size == 2000
    assert (w.folder / NAME / "video.mxf").exists()  # the department's folder is untouched
    draft = w.ctl.reply_draft(v.id)
    assert "backed up" in draft.body and NAME in draft.body


def test_a_path_outside_the_department_folder_is_ignored(w: World, tmp_path: Path) -> None:
    other = tmp_path / "elsewhere" / "Folder"
    (other / NAME).mkdir(parents=True)
    w.mail(body=BODY.format(folder=other))
    (v,) = w.ctl.views()
    assert v.source_path == str(w.folder / NAME)  # found in the department's own folder instead
    assert any("outside the allowed" in x for x in v.warnings)


def test_a_name_that_is_not_there_waits_for_the_person(w: World) -> None:
    w.mail(body="Please back up:\n- No Such Folder 2026\n")
    (v,) = w.ctl.views()
    assert v.state == "NEEDS_REVIEW" and "not found" in v.reason


def test_a_department_without_backups_gets_a_normal_request(w: World, tmp_path: Path) -> None:
    config = make_config(tmp_path, backup_enabled=False)
    assert not config.departments[0].backup_enabled
    w.ctl.apply_config(config, persist=False)
    w.mail()
    assert all(v.kind == "share" for v in w.ctl.views())


# --------------------------------------------------------------------------- already in the archive
def _archive_it_once(w: World) -> int:
    w.mail()
    (v,) = w.ctl.views()
    w.ctl.approve(v.id, confirmed=True)
    w.wait_copy(v.id)
    return v.id


def _second_request(w: World) -> int:
    w.mail(message_id="again")
    return next(v for v in w.ctl.views(["NEEDS_REVIEW"]) if v.archive_exists).id  # type: ignore[list-item]


def test_a_folder_already_in_the_archive_waits_for_a_choice(w: World) -> None:
    _archive_it_once(w)
    request_id = _second_request(w)
    v = w.ctl.view(request_id)
    assert v.state == "NEEDS_REVIEW" and "already in the archive" in v.reason
    with pytest.raises(ActionError, match="Only a Ready request"):
        w.ctl.approve(request_id, confirmed=True)


def test_keep_both_stores_it_under_a_dated_name(w: World) -> None:
    _archive_it_once(w)
    request_id = _second_request(w)
    w.ctl.archive_choice(request_id, "suffix")
    v = w.ctl.view(request_id)
    assert v.state == "READY"
    assert v.destination_path == str(w.archive / f"{NAME} ({TODAY.isoformat()})")
    w.ctl.approve(request_id, confirmed=True)
    w.wait_copy(request_id)
    assert (w.archive / f"{NAME} ({TODAY.isoformat()})" / "audio.wav").exists()
    assert (w.archive / NAME / "audio.wav").exists()  # the first backup is still there


def test_copy_only_new_or_changed_files_replaces_what_differs(w: World) -> None:
    _archive_it_once(w)
    (w.archive / NAME / "audio.wav").write_bytes(b"old")  # the archive has an older version
    (w.folder / NAME / "new.txt").write_bytes(b"n" * 30)  # and the department added a file
    request_id = _second_request(w)
    w.ctl.archive_choice(request_id, "update")
    assert w.ctl.view(request_id).replace_changed
    w.ctl.approve(request_id, confirmed=True)
    w.wait_copy(request_id)
    assert w.ctl.view(request_id).state == "VERIFIED"
    assert (w.archive / NAME / "audio.wav").stat().st_size == 500  # replaced
    assert (w.archive / NAME / "new.txt").exists()  # added
    assert (w.archive / NAME / "video.mxf").stat().st_size == 2000  # same, kept


def test_without_choosing_update_a_different_file_is_never_replaced(w: World) -> None:
    _archive_it_once(w)
    (w.archive / NAME / "audio.wav").write_bytes(b"old")
    request_id = _second_request(w)
    w.ctl.archive_choice(request_id, "suffix")  # a separate folder: the old file stays as it is
    w.ctl.approve(request_id, confirmed=True)
    w.wait_copy(request_id)
    assert (w.archive / NAME / "audio.wav").read_bytes() == b"old"


def test_skip_leaves_the_archive_alone(w: World) -> None:
    _archive_it_once(w)
    request_id = _second_request(w)
    w.ctl.archive_choice(request_id, "skip")
    assert w.ctl.view(request_id).state == "SKIPPED"


def test_there_is_nothing_to_choose_for_an_ordinary_request(w: World) -> None:
    w.mail()
    (v,) = w.ctl.views()
    with pytest.raises(ActionError, match="nothing to choose"):
        w.ctl.archive_choice(v.id, "skip")


# --------------------------------------------------------------------------- safety
def test_a_backup_is_never_copied_by_itself(w: World, tmp_path: Path) -> None:
    config = make_config(tmp_path).model_copy(update={"auto_copy_certain": True})
    w.ctl.apply_config(config, persist=False)
    w.mail()
    assert w.ctl.auto_copy_certain() == []
    (v,) = w.ctl.views()
    assert v.state == "READY"


def test_a_folder_browsed_outside_the_department_folder_is_refused(
    w: World, tmp_path: Path
) -> None:
    w.mail()
    (v,) = w.ctl.views()
    (tmp_path / "secret").mkdir()
    with pytest.raises(ActionError, match="cannot be backed up"):
        w.ctl.choose_path(v.id, str(tmp_path / "secret"))


def test_the_archive_cannot_be_the_source(w: World) -> None:
    w.mail()
    (v,) = w.ctl.views()
    w.archive.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ActionError, match="cannot be backed up"):
        w.ctl.choose_path(v.id, str(w.archive))


def test_a_stranger_can_only_name_folders_inside_the_allowed_ones(w: World, tmp_path: Path) -> None:
    config = make_config(tmp_path).model_copy(
        update={
            "backup": make_config(tmp_path).backup.model_copy(
                update={"allowed_sources": [str(w.dept_server)]}
            )
        }
    )
    w.ctl.apply_config(config, persist=False)
    email_id = w.repo.add_email(
        gmail_message_id="stranger", gmail_thread_id="t", sender_email="who@else.org",
        sender_name="Who", department=None, subject="Backup", snippet="x",
        received_at="2026-11-03T10:00:00+00:00", auth_status="verified",
    )  # fmt: skip
    assert email_id is not None
    ids = process_backup_requests(
        w.repo, config, email_id, "Backup", BODY.format(folder=w.folder), today=TODAY
    )
    (row,) = [w.repo.get_request(i) for i in ids]
    assert row is not None
    assert row["kind"] == "backup" and row["state"] == "NEEDS_REVIEW"  # never ready by itself
    assert row["department"] is None
    assert row["destination_path"] is None  # nothing is prefilled for a stranger
    view = w.ctl.view(ids[0])
    assert [c.path for c in view.candidates] == [str(w.folder / NAME)]
    w.ctl.choose_path(ids[0], view.candidates[0].path)  # the person picks it
    chosen = w.repo.get_request(ids[0])
    assert chosen is not None and chosen["state"] == "READY"
    assert chosen["destination_path"] == str(w.archive.parent / "general" / NAME)


def test_switching_a_backup_back_to_a_normal_request(w: World) -> None:
    w.mail()
    (v,) = w.ctl.views()
    w.ctl.switch_kind(v.email_id, "share")
    assert all(x.kind == "share" for x in w.ctl.views())
    w.ctl.switch_kind(v.email_id, "backup")
    assert all(x.kind == "backup" for x in w.ctl.views())

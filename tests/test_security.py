"""Phase 8: attacks that came out of the security review, each kept as a regression test."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox

from contentshare.app.controller import Controller
from contentshare.config.models import PUBLIC_MAIL_DOMAINS, AppConfig, Department
from contentshare.gmail.reply import build_reply
from contentshare.logsafe import SafeFormatter
from contentshare.parsing.request import parse_request
from contentshare.storage.db import Database
from contentshare.ui.bridge import Bridge
from contentshare.ui.dialogs import ReplyDialog
from contentshare.ui.main_window import MainWindow
from contentshare.ui.theme import apply_theme
from contentshare.ui.widgets import Label, SafeMessageBox
from fake_gmail import make_message
from helpers import TODAY, build_mail_world

HOSTILE = '<img src="//attacker.example/share/x.png"><a href="file:///C:/Windows/calc.exe">x</a>'


def dept(name: str, root: str, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("sender_emails", [f"{name.lower()}@dept.org"])
    return {"name": name, "destination_root": root, **kwargs}


# --------------------------------------------------------------------------- configuration
@pytest.mark.parametrize("domain", sorted(PUBLIC_MAIL_DOMAINS)[:6])
def test_public_mail_domains_cannot_be_allowlisted_as_a_whole(domain: str) -> None:
    with pytest.raises(ValidationError, match="public mail service"):
        Department(name="X", sender_domains=[domain], destination_root=r"D:\Out")


def test_a_specific_address_at_a_public_provider_is_still_allowed() -> None:
    d = Department(name="X", sender_emails=["anita@gmail.com"], destination_root=r"D:\Out")
    assert d.matches_sender("anita@gmail.com")
    assert not d.matches_sender("mallory@gmail.com")


@pytest.mark.parametrize(
    "dest",
    [
        r"D:\Archive",  # the same folder
        r"D:\archive\\",  # same, different case and trailing slash
        r"D:\Archive\Incoming",  # inside the archive
        "D:\\",  # the whole drive contains the archive
    ],
)
def test_copies_may_not_go_into_or_around_the_archive(dest: str) -> None:
    with pytest.raises(ValidationError, match="outside the archive"):
        AppConfig(source_roots=[r"D:\Archive"], departments=[dept("Media", dest)])


def test_sibling_folders_are_fine() -> None:
    config = AppConfig(
        source_roots=[r"D:\Archive"],
        departments=[dept("Media", r"D:\Archive2\Media"), dept("Finance", r"D:\Out\Finance")],
    )
    assert len(config.departments) == 2


@pytest.mark.parametrize("second", [r"E:\Out\Media", r"E:\out\media\Sub", r"E:\Out"])
def test_departments_may_not_share_or_nest_folders(second: str) -> None:
    with pytest.raises(ValidationError, match="own folder"):
        AppConfig(departments=[dept("Media", r"E:\Out\Media"), dept("Finance", second)])


def test_unc_folders_are_compared_too() -> None:
    with pytest.raises(ValidationError, match="own folder"):
        AppConfig(
            departments=[
                dept("A", r"\\nas\share\Incoming"),
                dept("B", r"\\NAS\Share\incoming\B"),
            ]
        )


def test_copies_may_not_go_into_windows_or_program_files(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
    for bad in (r"C:\Windows", r"C:\Windows\System32\x", r"c:\program files\Tool"):
        with pytest.raises(ValidationError, match="system folder"):
            Department(name="X", sender_emails=["a@b.org"], destination_root=bad)
    ok = Department(name="X", sender_emails=["a@b.org"], destination_root=r"C:\Windows2\Out")
    assert ok.destination_root.endswith("Out")


# --------------------------------------------------------------------------- email content
def test_subject_is_cut_before_parsing_so_a_huge_one_cannot_stall_the_app() -> None:
    start = time.perf_counter()
    result = parse_request("a" * 2_000_000, "please share Sunrise Talk 12 Oct 2026", today=TODAY)
    assert time.perf_counter() - start < 2.0
    assert result.items


@pytest.mark.parametrize(
    "body",
    [
        "a" * 1_000_000,
        "12/10/" * 200_000,
        "<div>" * 100_000 + "x",
        "\n".join(f"- item {i} 12 Oct 2026" for i in range(50_000)),
        "(" * 100_000,
        "> " * 300_000,
        "x@" + "a." * 200_000,
        "http://" + "a" * 500_000,
    ],
)
def test_hostile_bodies_parse_quickly(body: str) -> None:
    start = time.perf_counter()
    parse_request("subject", body, today=TODAY)
    assert time.perf_counter() - start < 2.0


def test_reply_headers_cannot_be_injected() -> None:
    kwargs: dict[str, Any] = {
        "to_addr": "anita@dept.org",
        "subject": "Re: ok",
        "body": "hi",
        "token": "t",
        "original_message_id": "<a@b>",
        "original_references": None,
    }
    for field, value in (
        ("to_addr", "anita@dept.org\r\nBcc: evil@example.org"),
        ("subject", "Re: ok\r\nBcc: evil@example.org"),
        ("token", "t\r\nBcc: evil@example.org"),
    ):
        with pytest.raises(ValueError, match="carriage return"):
            build_reply(**{**kwargs, field: value}).as_bytes()


# --------------------------------------------------------------------------- logging
def test_log_never_contains_exception_text_from_an_email(tmp_path: Path) -> None:
    secret = "Dear Anita, the budget password is hunter2"
    path = tmp_path / "log.txt"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(SafeFormatter("%(levelname)s %(message)s"))
    logger = logging.getLogger("contentshare.test-secure-log")
    logger.addHandler(handler)
    try:
        try:
            try:
                raise KeyError(secret)
            except KeyError as inner:
                raise RuntimeError(f"cannot parse: {secret}") from inner
        except RuntimeError:
            logger.exception("unexpected error handling message %s", "abc123")
        try:
            Path(tmp_path / "missing" / "f.txt").read_text()
        except OSError:
            logger.exception("disk problem")
    finally:
        handler.close()
        logger.removeHandler(handler)
    text = path.read_text(encoding="utf-8")
    assert "hunter2" not in text
    assert "Dear Anita" not in text
    assert "abc123" in text  # our own message (ids only) is kept
    assert "RuntimeError" in text
    assert "KeyError" in text  # the chain shows what happened without the content
    assert "error code" in text  # OS errors keep their numeric code
    assert "test_security.py" in text  # and where it happened


# --------------------------------------------------------------------------- plain text only
def test_labels_show_html_literally() -> None:
    label = Label(HOSTILE)
    assert label.textFormat() == Qt.TextFormat.PlainText
    label.setText(HOSTILE)
    assert label.textFormat() == Qt.TextFormat.PlainText
    assert label.text() == HOSTILE


def test_message_boxes_are_plain_text(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[Qt.TextFormat, str]] = []

    def fake_exec(self: QMessageBox) -> int:
        seen.append((self.textFormat(), self.text()))
        return int(QMessageBox.StandardButton.Yes)

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    for show in (
        SafeMessageBox.warning,
        SafeMessageBox.information,
        SafeMessageBox.critical,
        SafeMessageBox.question,
    ):
        show(None, "t", HOSTILE)
    assert [fmt for fmt, _ in seen] == [Qt.TextFormat.PlainText] * 4
    assert all(text == HOSTILE for _, text in seen)
    answer = SafeMessageBox.question(None, "t", "ok?")
    assert answer == QMessageBox.StandardButton.Yes


def test_no_screen_uses_the_default_label_or_message_box() -> None:
    """Every screen must import ``Label`` and ``SafeMessageBox`` from ``ui.widgets``."""
    ui = Path(__file__).parent.parent / "src" / "contentshare" / "ui"
    offenders = []
    for path in ui.glob("*.py"):
        if path.name == "widgets.py":
            continue
        text = path.read_text(encoding="utf-8")
        imported = " ".join(
            re.findall(r"from PySide6\.QtWidgets import \(?([^)]*?)\)?\n\n", text, re.S)
        )
        for name in ("QLabel", "QMessageBox"):
            if re.search(rf"\b{name}\b", imported):
                offenders.append(f"{path.name}: {name}")
    assert offenders == []


class Rig:
    def __init__(self, tmp_path: Path, db: Database) -> None:
        self.world = build_mail_world(tmp_path, db)
        self.bridge = Bridge()
        self.controller = Controller(
            self.world.repo, self.world.index, self.world.config,
            gateway_factory=lambda: self.world.gateway,
            config_path=tmp_path / "config.yaml", today=lambda: TODAY,
        )  # fmt: skip
        self.window = MainWindow(self.controller, self.bridge)


@pytest.fixture
def rig(qtbot: Any, tmp_path: Path, db: Database) -> Iterator[Rig]:
    apply_theme(QApplication.instance())  # type: ignore[arg-type]
    r = Rig(tmp_path, db)
    qtbot.addWidget(r.window)
    yield r
    r.controller.shutdown()
    r.bridge.close()


def test_a_hostile_email_is_shown_as_text_everywhere(rig: Rig, qtbot: Any) -> None:
    rig.world.gmail.add(
        make_message(
            message_id="h1",
            from_='"<b>Anita</b> <i>R.</i>" <anita@dept.org>',
            subject=f"{HOSTILE} Sunrise Talk 12 Oct 2026",
            body=f"{HOSTILE}\nplease share Sunrise Talk English 12 Oct 2026",
        )
    )
    rig.controller.check_now()
    rig.window.show()
    rig.window.requests.refresh()
    assert rig.window.requests._cards, "the hostile email should still produce a card"
    labels = rig.window.findChildren(QLabel)
    assert labels
    shown = [lbl for lbl in labels if "attacker.example" in lbl.text()]
    assert shown, "the hostile text should be on screen as text"
    assert all(lbl.textFormat() == Qt.TextFormat.PlainText for lbl in shown)
    # Row captions that Qt's form layout creates itself hold fixed words, never email text;
    # no other label may be able to render markup.
    rich = [
        lbl.text()[:40]
        for lbl in labels
        if lbl.textFormat() != Qt.TextFormat.PlainText and re.search(r"[<>&]", lbl.text())
    ]
    assert rich == []
    # the dialog that previews the outgoing reply shows the original subject too
    view = rig.controller.views()[0]
    from contentshare.gmail.replies import ReplyDraft

    draft = ReplyDraft(view.id, "shared", "anita@dept.org", f"Re: {HOSTILE}", HOSTILE, "t1")
    dialog = ReplyDialog(draft, summary=HOSTILE)
    qtbot.addWidget(dialog)
    carrying = [lbl for lbl in dialog.findChildren(QLabel) if "attacker.example" in lbl.text()]
    assert carrying, "the subject and summary should be shown"
    assert all(lbl.textFormat() == Qt.TextFormat.PlainText for lbl in carrying)


# --------------------------------------------------------------------------- never destructive
def test_the_engine_refuses_to_delete_anything_but_its_own_temp_files(tmp_path: Path) -> None:
    from contentshare.copy_engine.engine import PARTIAL_SUFFIX, _quiet_unlink

    real = tmp_path / "Program_part1.mxf"
    real.write_bytes(b"the only copy of something important")
    _quiet_unlink(str(real))
    assert real.exists()
    temp = tmp_path / f"Program_part1.mxf{PARTIAL_SUFFIX}"
    temp.write_bytes(b"partial")
    _quiet_unlink(str(temp))
    assert not temp.exists()
    _quiet_unlink(str(tmp_path / "missing.cspartial"))  # already gone: fine


def test_cleanup_only_removes_partial_files(tmp_path: Path) -> None:
    from contentshare.copy_engine import cleanup_partials

    (tmp_path / "a.mxf").write_bytes(b"x")
    (tmp_path / "b.mxf.cspartial").write_bytes(b"x")
    (tmp_path / "c.cspartial.mxf").write_bytes(b"x")  # not ours: the suffix is not last
    cleanup_partials(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.mxf", "c.cspartial.mxf"]

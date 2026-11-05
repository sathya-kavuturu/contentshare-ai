"""One email is one request on screen, with a section for each folder it asked for.

Each folder is still a request line of its own underneath (so it can be chosen, copied and checked
on its own); these tests cover what the person sees and does with the email as a whole.
"""

# ruff: noqa: F811  (the fixtures are imported, and tests take them as arguments)
from __future__ import annotations

import email
import threading
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QDialog, QPushButton, QScrollArea

from contentshare.app.controller import ActionError
from contentshare.ui import request_card as card_module
from contentshare.ui.request_card import group_status
from fake_gmail import make_message
from test_controller import Harness, h  # noqa: F401  (the controller fixture)
from test_exact_names import EXACT, _exact
from test_folders_help import Rig, rig  # noqa: F401  (the window fixture)

SECOND = "2026-10-12_SunriseTalk_HI"  # another single folder on the test server
LIST = f"Please share these:\n- {EXACT}\n- Twin_Event\n- 2026-10-13_NoSuchTalk_EN\n"


def _twins(storage: Path, count: int, name: str = "Twin_Event") -> None:
    for number in range(count):
        folder = storage / f"copy_{number:02d}" / name
        folder.mkdir(parents=True)
        (folder / "a.mp4").write_bytes(b"x" * (10 + number))


def _list_email(h: Harness, twins: int = 2) -> list[int]:
    _twins(h.world.storage, twins)
    h.world.index.scan_all([str(h.world.storage)])
    _exact(h)
    h.mail(message_id="g1", subject="Folders for Monday", body=LIST)
    return [v.id for v in sorted(h.ctl.views(), key=lambda v: v.id)]


def _exact_on(rig: Rig) -> None:
    departments = [
        d.model_copy(update={"exact_folder_names": True}) for d in rig.controller.config.departments
    ]
    config = rig.controller.config.model_copy(update={"departments": departments})
    rig.controller.apply_config(config, persist=False)


def _buttons(card: Any, start: str) -> list[QPushButton]:
    return [b for b in card.findChildren(QPushButton) if b.text().startswith(start)]


# --------------------------------------------------------------------------- one request
def test_a_list_in_one_email_is_one_group_with_a_line_for_each_folder(h: Harness) -> None:
    ids = _list_email(h)
    (group,) = h.ctl.groups()
    assert [v.id for v in group.items] == ids and group.many
    assert group.lead.id == ids[0]
    assert [v.state for v in group.items] == ["READY", "NEEDS_REVIEW", "NEEDS_REVIEW"]
    assert sum(h.ctl.counts().values()) == 3  # three lines underneath, one request on screen
    assert h.ctl.attention_count() == 1


def test_an_email_with_one_folder_is_a_group_of_one(h: Harness) -> None:
    h.ready()
    (group,) = h.ctl.groups()
    assert not group.many and group.items == (h.ctl.views()[0],)


def test_a_group_is_listed_when_any_line_is_in_the_asked_for_state(h: Harness) -> None:
    _list_email(h)
    assert len(h.ctl.groups(["READY"])) == 1
    assert len(h.ctl.groups(["VERIFIED"])) == 0
    (group,) = h.ctl.groups(["READY"])
    assert len(group.items) == 3  # shown whole, not only the line that matched


def test_two_emails_are_two_groups(h: Harness) -> None:
    _exact(h)
    h.mail(message_id="a", subject="One", body=EXACT)
    h.mail(message_id="b", subject="Two", body=f"x\n{EXACT}")
    assert len(h.ctl.groups()) == 2


def test_the_status_chip_tells_the_story_of_the_whole_email(h: Harness) -> None:
    _list_email(h)
    (group,) = h.ctl.groups()
    assert group_status(group) == ("⚠ 2 of 3 need review", "warn")


# --------------------------------------------------------------------------- all the paths
def test_every_path_with_the_requested_name_is_kept_for_choosing(h: Harness) -> None:
    _twins(h.world.storage, 9)
    h.world.index.scan_all([str(h.world.storage)])
    _exact(h)
    h.mail(message_id="t9", subject="Twin", body="Twin_Event")
    (view,) = h.ctl.views()
    assert view.state == "NEEDS_REVIEW" and len(view.candidates) == 9  # not cut at five
    assert len({c.path for c in view.candidates}) == 9


def test_similar_matches_are_not_cut_at_five_either(h: Harness) -> None:
    for number in range(8):
        folder = h.world.storage / f"Sunrise Talk 12 Oct 2026 take {number}"
        folder.mkdir()
        (folder / "a.mp4").write_bytes(b"x" * 20)
    h.world.index.scan_all([str(h.world.storage)])
    h.mail(message_id="s8", subject="Sunrise Talk 12 Oct 2026", body="english please")
    (view,) = h.ctl.views()
    assert len(view.candidates) > 5


def test_the_card_lists_all_the_paths_and_using_one_makes_the_line_ready(
    rig: Rig, qtbot: Any
) -> None:
    _twins(rig.world.storage, 3)
    rig.world.index.scan_all([str(rig.world.storage)])
    _exact_on(rig)
    rig.world.gmail.add(make_message(message_id="t3", subject="Twin", body="Twin_Event"))
    rig.controller.check_now()
    rig.window.requests.refresh()
    (card,) = rig.window.requests._cards
    uses = _buttons(card, "Use this")
    assert len(uses) == 3
    assert {b.accessibleName() for b in uses} == {f"Use {c.path}" for c in card.view.candidates}
    uses[1].click()
    qtbot.waitUntil(lambda: rig.controller.views()[0].state == "READY", timeout=10000)
    assert rig.controller.views()[0].source_path == card.view.candidates[1].path


def test_a_long_list_of_paths_scrolls_inside_the_card(rig: Rig) -> None:
    _twins(rig.world.storage, 9)
    rig.world.index.scan_all([str(rig.world.storage)])
    _exact_on(rig)
    rig.world.gmail.add(make_message(message_id="t9", subject="Twin", body="Twin_Event"))
    rig.controller.check_now()
    rig.window.requests.refresh()
    (card,) = rig.window.requests._cards
    assert len(_buttons(card, "Use this")) == 9  # every one is there, none is left out
    assert card.findChildren(QScrollArea)


# --------------------------------------------------------------------------- the card
def test_one_card_for_the_email_with_a_section_for_each_folder(rig: Rig) -> None:
    _twins(rig.world.storage, 2)
    rig.world.index.scan_all([str(rig.world.storage)])
    _exact_on(rig)
    rig.world.gmail.add(make_message(message_id="g2", subject="Folders for Monday", body=LIST))
    rig.controller.check_now()
    rig.window.requests.refresh()
    cards = rig.window.requests._cards
    assert len(cards) == 1  # not three
    card = cards[0]
    assert card.group.many and len(card.group.items) == 3
    assert card.status.text() == "⚠ 2 of 3 need review"
    shown = card.details.findChildren(type(card.status))
    texts = [w.text() for w in shown]
    assert any(t.startswith("1.") and EXACT in t for t in texts)
    assert any(t.startswith("2.") and "Twin_Event" in t for t in texts)
    assert any(t.startswith("3.") and "NoSuchTalk" in t for t in texts)
    rig.window.update_chrome()
    assert rig.window.nav.item(0).text() == "Requests (1)"  # one email, not three lines


def test_the_card_approves_the_ready_folders_together(rig: Rig, qtbot: Any) -> None:
    _exact_on(rig)
    rig.world.gmail.add(make_message(message_id="g3", subject="Two", body=f"- {EXACT}\n- {SECOND}"))
    rig.controller.check_now()
    rig.window.requests.refresh()
    (card,) = rig.window.requests._cards
    assert [v.state for v in card.group.items] == ["READY", "READY"]
    (approve,) = _buttons(card, "Approve and copy")
    assert approve.text() == "Approve and copy (2 ready)"
    card.confirm.setChecked(True)
    approve.click()
    qtbot.waitUntil(
        lambda: (
            all(v.state == "VERIFIED" for v in rig.controller.views()) and not rig.controller.busy()
        ),
        timeout=30000,
    )
    rig.window.requests.refresh()
    assert rig.window.requests._cards[0].status.text() == "✔✔ 2 of 2 verified"


def test_the_buttons_for_the_email_as_a_whole(rig: Rig) -> None:
    _twins(rig.world.storage, 2)
    rig.world.index.scan_all([str(rig.world.storage)])
    _exact_on(rig)
    rig.world.gmail.add(make_message(message_id="g4", subject="Folders", body=LIST))
    rig.controller.check_now()
    rig.window.requests.refresh()
    (card,) = rig.window.requests._cards
    assert _buttons(card, "Skip all") and _buttons(card, "Reject")
    assert len(_buttons(card, "Skip this folder")) == 3  # and one per folder


# --------------------------------------------------------------------------- the whole email
def test_approve_group_needs_the_tick_and_something_ready(h: Harness) -> None:
    ids = _list_email(h)
    email_id = h.ctl.views()[0].email_id
    with pytest.raises(ActionError, match="Confirm copy"):
        h.ctl.approve_group(email_id, confirmed=False)
    h.ctl.skip(ids[0])  # the only Ready line is gone
    with pytest.raises(ActionError, match="Ready"):
        h.ctl.approve_group(email_id, confirmed=True)


def test_approve_group_copies_each_ready_folder_and_reports_a_bad_one(
    h: Harness,
) -> None:
    _exact(h)
    h.mail(message_id="g5", subject="Two", body=f"- {EXACT}\n- {SECOND}")
    a, b = sorted(h.ctl.views(), key=lambda v: v.id)
    # the second folder disappears from the server before the approval
    import shutil

    shutil.rmtree(b.source_path or "")
    result = h.ctl.approve_group(a.email_id, confirmed=True)
    assert result.started == 1
    assert len(result.problems) == 1 and "no longer on the storage server" in result.problems[0]
    h.wait_copy(a.id)
    assert h.ctl.view(a.id).state == "VERIFIED"
    assert h.ctl.view(b.id).state == "READY"  # still waiting, nothing half done


def test_skip_group_skips_every_open_line_and_only_those(h: Harness) -> None:
    ids = _list_email(h)
    email_id = h.ctl.views()[0].email_id
    assert h.ctl.skip_group(email_id) == ids
    assert {v.state for v in h.ctl.views()} == {"SKIPPED"}
    with pytest.raises(ActionError):
        h.ctl.skip_group(email_id)


def test_reject_group_rejects_the_lines_not_yet_approved(h: Harness) -> None:
    ids = _list_email(h)
    email_id = h.ctl.views()[0].email_id
    assert h.ctl.reject_group(email_id, "not available") == ids
    assert {v.state for v in h.ctl.views()} == {"REJECTED"}


# --------------------------------------------------------------------------- one reply
def _verify_two(h: Harness) -> tuple[int, int, int]:
    _exact(h)
    h.mail(message_id="g6", subject="Two folders", body=f"- {EXACT}\n- {SECOND}")
    a, b = sorted(h.ctl.views(), key=lambda v: v.id)
    h.ctl.approve_group(a.email_id, confirmed=True)
    h.wait_copy(a.id)
    h.wait_copy(b.id)
    assert {v.state for v in h.ctl.views()} == {"VERIFIED"}
    return a.email_id, a.id, b.id


def test_one_reply_answers_every_verified_folder_of_the_email(h: Harness) -> None:
    email_id, first, second = _verify_two(h)
    draft = h.ctl.reply_draft_for_email(email_id)
    assert draft.request_id == first and draft.also == (second,)
    assert EXACT in draft.body and SECOND in draft.body
    assert "files" in draft.body
    result = h.ctl.send_reply(draft)
    assert result.status == "SENT"
    assert len(h.world.gmail.sent) == 1  # one message, not two
    assert {v.state for v in h.ctl.views()} == {"REPLIED"}
    replies = h.world.repo.list_replies()
    assert len(replies) == 2 and {r["status"] for r in replies} == {"SENT"}
    assert len({r["token"] for r in replies}) == 1  # the same message


def test_the_reply_cannot_be_sent_twice_for_the_same_email(h: Harness) -> None:
    email_id, _first, _second = _verify_two(h)
    draft = h.ctl.reply_draft_for_email(email_id)
    h.ctl.send_reply(draft)
    with pytest.raises(ActionError):
        h.ctl.reply_draft_for_email(email_id)  # nothing left to answer
    with pytest.raises(ActionError):
        h.ctl.send_reply(draft)
    assert len(h.world.gmail.sent) == 1


def test_a_folder_not_yet_verified_is_left_out_of_the_reply(h: Harness) -> None:
    ids = _list_email(h)
    email_id = h.ctl.views()[0].email_id
    h.ctl.approve_group(email_id, confirmed=True)  # only the first line is Ready
    h.wait_copy(ids[0])
    draft = h.ctl.reply_draft_for_email(email_id)
    assert draft.also == ()
    assert "Twin_Event" not in draft.body and EXACT in draft.body


def test_the_reply_goes_in_the_thread_to_the_sender_with_the_original_subject(
    h: Harness,
) -> None:
    email_id, _first, _second = _verify_two(h)
    h.ctl.send_reply(h.ctl.reply_draft_for_email(email_id))
    sent = h.world.gmail.sent[0]
    message = email.message_from_bytes(sent["raw"])
    assert message["Subject"] == "Re: Two folders"
    assert "anita@dept.org" in message["To"] or "editor@dept.org" in message["To"]


def test_automatic_reply_waits_for_the_last_folder_and_is_sent_once(h: Harness) -> None:
    _exact(h)
    h.mail(message_id="g7", subject="Two folders", body=f"- {EXACT}\n- {SECOND}")
    a, b = sorted(h.ctl.views(), key=lambda v: v.id)
    h.ctl.set_auto_reply_group(a.email_id, True)
    assert all(v.auto_reply for v in h.ctl.views())
    h.ctl.approve_group(a.email_id, confirmed=True)
    h.wait_copy(a.id)
    h.wait_copy(b.id)
    # whichever folder finishes last sends the one reply
    assert h.replied.setdefault(b.id, threading.Event()).wait(30) or h.replied.setdefault(
        a.id, threading.Event()
    ).wait(30)
    assert {v.state for v in h.ctl.views()} == {"REPLIED"}
    assert len(h.world.gmail.sent) == 1


def test_automatic_reply_does_not_wait_for_a_folder_nobody_has_started(
    h: Harness,
) -> None:
    ids = _list_email(h)
    email_id = h.ctl.views()[0].email_id
    h.ctl.set_auto_reply_group(email_id, True)
    h.ctl.approve_group(email_id, confirmed=True)  # only the first line is Ready
    h.wait_copy(ids[0])
    h.wait_reply(ids[0])
    states = {v.id: v.state for v in h.ctl.views()}
    assert states[ids[0]] == "REPLIED" and states[ids[1]] == "NEEDS_REVIEW"
    assert len(h.world.gmail.sent) == 1
    body = email.message_from_bytes(h.world.gmail.sent[0]["raw"]).get_payload(decode=True)
    assert b"Twin_Event" not in body and EXACT.encode() in body


def test_the_card_sends_one_reply_for_the_email(
    rig: Rig, qtbot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _exact_on(rig)
    rig.world.gmail.add(make_message(message_id="g9", subject="Two", body=f"- {EXACT}\n- {SECOND}"))
    rig.controller.check_now()
    rig.window.requests.refresh()
    (card,) = rig.window.requests._cards
    card.confirm.setChecked(True)
    _buttons(card, "Approve and copy")[0].click()
    qtbot.waitUntil(
        lambda: (
            all(v.state == "VERIFIED" for v in rig.controller.views()) and not rig.controller.busy()
        ),
        timeout=30000,
    )
    rig.window.requests.refresh()
    (card,) = rig.window.requests._cards

    class Accept:
        def __init__(self, draft: Any, **_k: Any) -> None:
            self._draft = draft
            self.summary = _k["summary"]
            seen.append(self)

        def exec(self) -> int:
            return QDialog.DialogCode.Accepted

        def draft(self) -> Any:
            return self._draft

    seen: list[Accept] = []
    monkeypatch.setattr(card_module, "ReplyDialog", Accept)
    rig.controller.config.dry_run = False
    _buttons(card, "Preview and send reply")[0].click()
    qtbot.waitUntil(
        lambda: all(v.state == "REPLIED" for v in rig.controller.views()), timeout=10000
    )
    assert len(rig.world.gmail.sent) == 1
    assert "2 folders" in seen[0].summary


def test_the_reply_lists_each_folder_on_its_own_line_with_no_path(h: Harness) -> None:
    email_id, _first, _second = _verify_two(h)
    draft = h.ctl.reply_draft_for_email(email_id)
    lines = draft.body.splitlines()
    assert f"  - {EXACT}" in lines and f"  - {SECOND}" in lines  # one folder per line
    assert str(h.world.media) not in draft.body and str(h.world.storage) not in draft.body
    assert chr(92) not in draft.body  # no network path


def test_a_folder_added_by_hand_is_named_in_the_reply_too(h: Harness) -> None:
    email_id, _first, _second = _verify_two(h)
    (h.world.storage / "Weekly" / "Bonus Talk").mkdir(parents=True)
    (h.world.storage / "Weekly" / "Bonus Talk" / "a.mp4").write_bytes(b"x")
    h.world.index.scan_all([str(h.world.storage)])
    added = h.ctl.add_folder(email_id, "Bonus")
    (hit,) = [c for c in h.ctl.search(added, "Bonus Talk") if c.item_id is not None][:1]
    h.ctl.choose(added, hit.item_id)  # type: ignore[arg-type]
    h.ctl.approve(added, confirmed=True)
    h.wait_copy(added)
    draft = h.ctl.reply_draft_for_email(email_id)
    lines = draft.body.splitlines()
    assert [f"  - {EXACT}", f"  - {SECOND}", "  - Bonus Talk"] == [
        x for x in lines if x.startswith("  - ")
    ]

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from contentshare.config.models import AppConfig, ParsingSettings
from contentshare.index import ContentIndex, IndexedItem
from contentshare.jobs.states import RequestState
from contentshare.matching import (
    destination_for,
    process_email_requests,
    program_label,
    rank_candidates,
    score_item,
    triage_item,
)
from contentshare.matching.scorer import MIN_SHOWN, nested
from contentshare.parsing.request import RequestItem, parse_request
from contentshare.storage.db import Database
from contentshare.storage.repository import Repository
from helpers import make_storage

S = RequestState
TODAY = date(2026, 11, 3)


@pytest.fixture
def world(tmp_path: Path, db: Database):  # type: ignore[no-untyped-def]
    storage = make_storage(tmp_path / "Programs")
    media = tmp_path / "media_incoming"
    media.mkdir()
    config = AppConfig(
        source_roots=[str(storage)],
        departments=[
            {
                "name": "Media Team",
                "sender_emails": ["anita@dept.org"],
                "destination_root": str(media),
            },
            {
                "name": "Finance",
                "sender_domains": ["finance.org"],
                "destination_root": str(tmp_path / "finance"),
                "subfolder_pattern": "{program}/{date}",
            },
        ],
    )
    index = ContentIndex(db, config.index)
    index.scan_root(str(storage))
    return storage, media, config, index


def first_item(subject: str, body: str = "") -> RequestItem:
    return parse_request(subject, body, ParsingSettings(), today=TODAY).items[0]


def triage(world: Any, subject: str, body: str = ""):  # type: ignore[no-untyped-def]
    _, _, config, index = world
    return triage_item(first_item(subject, body), index, config, today=TODAY)


# ------------------------------------------------------------------ decisions
def test_exact_request_is_ready(world: Any) -> None:
    result = triage(world, "Sunrise Talk 12 Oct 2026 English recording")
    assert result.state == S.READY
    assert result.chosen is not None
    assert result.chosen.item.name == "2026-10-12_SunriseTalk_EN"
    assert result.chosen.score >= 85
    assert "date matches" in result.chosen.notes


def test_missing_language_is_ambiguous_and_goes_to_review(world: Any) -> None:
    result = triage(world, "Sunrise Talk 12 Oct 2026")
    assert result.state == S.NEEDS_REVIEW
    assert result.chosen is None
    assert {c.item.name for c in result.candidates[:2]} == {
        "2026-10-12_SunriseTalk_EN",
        "2026-10-12_SunriseTalk_HI",
    }
    assert "similar" in result.reason


def test_wrong_date_is_never_ready(world: Any) -> None:
    result = triage(world, "Sunrise Talk 13 Oct 2026 English")
    assert result.state == S.NEEDS_REVIEW
    assert "differs" in result.reason
    assert result.candidates  # the near misses are still offered


def test_wrong_language_is_never_ready(world: Any) -> None:
    result = triage(world, "Sunrise Talk 19 Oct 2026 Hindi")  # only an English one exists
    assert result.state == S.NEEDS_REVIEW
    assert "language" in result.reason


def test_typo_still_finds_the_right_folder(world: Any) -> None:
    result = triage(world, "Sunrize Talk 12 Oct 2026 EN")
    assert result.state == S.READY
    assert result.chosen is not None
    assert result.chosen.item.name == "2026-10-12_SunriseTalk_EN"


def test_part_number_picks_the_right_day(world: Any) -> None:
    result = triage(world, "Youth Retreat day 2")
    assert result.state == S.READY
    assert result.chosen is not None
    assert result.chosen.item.name == "YouthRetreat_2026-09-04_Day2"


def test_missing_part_number_is_ambiguous(world: Any) -> None:
    result = triage(world, "Youth Retreat recording")
    assert result.state == S.NEEDS_REVIEW
    assert {c.item.name for c in result.candidates} >= {
        "YouthRetreat_2026-09-03_Day1",
        "YouthRetreat_2026-09-04_Day2",
    }


def test_wrong_part_number_is_flagged(world: Any) -> None:
    result = triage(world, "Youth Retreat day 5")
    assert result.state == S.NEEDS_REVIEW
    assert "different day" in result.reason


def test_unknown_program_has_no_candidates(world: Any) -> None:
    result = triage(world, "Quantum Physics Lecture")
    assert result.state == S.NEEDS_REVIEW
    assert result.candidates == []
    assert "No content found" in result.reason


def test_request_without_a_program_is_flagged(world: Any) -> None:
    result = triage(world, "Hi", "Hi, can you help me?")
    assert result.state == S.NEEDS_REVIEW
    assert "which program" in result.reason


def test_folder_with_content_beats_the_files_inside_it(world: Any) -> None:
    result = triage(world, "Annual Day 2025")
    assert result.state == S.READY
    assert result.chosen is not None
    assert result.chosen.item.name == "AnnualDay_2025-12-25"
    assert result.chosen.item.is_dir
    assert not any(nested(result.chosen.item, c.item) for c in result.candidates[1:])


def test_single_file_can_be_the_match(world: Any) -> None:
    result = triage(world, "Morning Satsang 12 Oct 2026 audio")
    assert result.state == S.READY
    assert result.chosen is not None
    assert result.chosen.item.name == "2026-10-12 Morning Satsang audio.mp3"
    assert "date matches" in result.chosen.notes


def test_asking_for_audio_does_not_choose_video(world: Any) -> None:
    items = world[3].search("Sunrise Talk")
    req = first_item("Sunrise Talk 12 Oct 2026 EN audio")
    video = next(i for i in items if i.name == "video.mxf" and "12_SunriseTalk_EN" in i.rel_path)
    audio = next(i for i in items if i.name == "audio.wav")
    s_video = score_item(req, video, ParsingSettings(), today=TODAY)
    s_audio = score_item(req, audio, ParsingSettings(), today=TODAY)
    assert "format_mismatch" in s_video.flags
    assert "format_match" in s_audio.flags
    assert s_audio.raw_score > s_video.raw_score


def test_candidates_are_limited_and_sorted(world: Any) -> None:
    result = triage(world, "Sunrise Talk")
    scores = [c.raw_score for c in result.candidates]
    assert scores == sorted(scores, reverse=True)
    assert len(result.candidates) <= 5
    assert all(c.score >= MIN_SHOWN for c in result.candidates)


def test_thresholds_come_from_settings(world: Any) -> None:
    _, _, config, index = world
    item = first_item("Annual Dai 2025")  # a typo: about 89% sure
    assert triage_item(item, index, config, today=TODAY).state == S.READY
    strict = config.model_copy(
        update={"matching": config.matching.model_copy(update={"auto_ready_score": 95})}
    )
    result = triage_item(item, index, strict, today=TODAY)
    assert result.state == S.NEEDS_REVIEW
    assert "89% sure" in result.reason


def test_matching_date_and_language_cannot_rescue_a_vague_name(world: Any) -> None:
    result = triage(world, "Talk 12 Oct 2026 EN")
    assert result.candidates[0].item.name == "2026-10-12_SunriseTalk_EN"
    assert result.candidates[0].score >= 85  # the bonuses push the total up...
    assert result.state == S.NEEDS_REVIEW  # ...but the name alone is not convincing
    assert "sure" in result.reason


def test_candidate_dict_is_json_ready(world: Any) -> None:
    result = triage(world, "Sunrise Talk 12 Oct 2026")
    json.dumps([c.to_dict() for c in result.candidates])


# ------------------------------------------------------------------ destinations
def test_destination_uses_the_departments_pattern(world: Any, tmp_path: Path) -> None:
    _, media, config, _ = world
    result = triage(world, "Sunrise Talk 12 Oct 2026 EN")
    assert result.chosen is not None
    media_dept, finance = config.departments
    item = first_item("Sunrise Talk 12 Oct 2026 EN")
    assert (
        destination_for(media_dept, result.chosen, item)
        == (media / "2026-10-12_SunriseTalk_EN").resolve()
    )
    dest = destination_for(finance, result.chosen, item)
    assert dest == (tmp_path / "finance" / "2026-10-12_SunriseTalk_EN" / "2026-10-12").resolve()
    no_date = first_item("Sunrise Talk English")
    assert (
        destination_for(finance, result.chosen, no_date)
        == (tmp_path / "finance" / "2026-10-12_SunriseTalk_EN").resolve()
    )  # an empty {date} simply disappears


def test_file_matches_use_the_parent_folder_name(world: Any) -> None:
    result = triage(world, "Morning Satsang 12 Oct 2026 audio")
    assert result.chosen is not None
    assert program_label(result.chosen) == "Satsang Morning (Caf\u00e9)"


# ------------------------------------------------------------------ rank helpers
def test_nested_detection() -> None:
    def item(rel: str, root: str = "R") -> IndexedItem:
        return IndexedItem(1, root, rel, rel.rsplit("/", 1)[-1], "dir", 0, 0, 0, rel.count("/") + 1)

    assert nested(item("a/b"), item("a"))
    assert nested(item("a"), item("a/b"))
    assert not nested(item("a/b"), item("a/c"))
    assert not nested(item("ab"), item("a"))  # a name prefix is not a parent
    assert not nested(item("a/b", "R1"), item("a", "R2"))


def test_weak_matches_are_not_shown(world: Any) -> None:
    ranked = rank_candidates(
        first_item("Zzzz Qqqq"), world[3].search("Zzzz Qqqq"), ParsingSettings(), today=TODAY
    )
    assert ranked == []


# ------------------------------------------------------------------ whole email -> requests
def add_email(
    repo: Repository, sender: str = "anita@dept.org", auth: str = "verified", msg: str = "m1"
) -> int:
    email_id = repo.add_email(
        gmail_message_id=msg, gmail_thread_id="t", sender_email=sender,
        received_at="2026-11-03T08:00:00+00:00", auth_status=auth, department="Media Team",
    )  # fmt: skip
    assert email_id is not None
    return email_id


def test_email_with_two_items_creates_two_lines(world: Any, repo: Repository) -> None:
    _, media, config, index = world
    email_id = add_email(repo)
    ids = process_email_requests(
        repo, index, config, email_id, "Need recordings",
        "Please share:\n- Sunrise Talk 12 Oct 2026 EN\n- Quantum Lecture\n", today=TODAY,
    )  # fmt: skip
    ready, review = (repo.get_request(i) for i in ids)
    assert ready is not None
    assert review is not None
    assert ready["state"] == "READY"
    assert ready["source_path"].endswith("2026-10-12_SunriseTalk_EN")
    assert ready["destination_path"] == str((media / "2026-10-12_SunriseTalk_EN").resolve())
    assert (ready["file_count"], ready["total_bytes"]) == (3, 2510)
    assert ready["confidence"] >= 85
    detail = json.loads(ready["query_json"])
    assert detail["item"]["program"] == "Sunrise Talk"
    assert detail["candidates"][0]["rel_path"].endswith("2026-10-12_SunriseTalk_EN")

    assert review["state"] == "NEEDS_REVIEW"
    assert review["source_path"] is None  # nothing is pre-selected for review
    assert review["destination_path"] is None
    assert "No content found" in json.loads(review["query_json"])["reason"]
    assert [e["event"] for e in repo.audit_entries(ids[0])][-1] == "state_changed"


def test_review_candidates_are_stored_but_not_preselected(world: Any, repo: Repository) -> None:
    _, _, config, index = world
    (rid,) = process_email_requests(
        repo, index, config, add_email(repo), "Sunrise Talk 12 Oct 2026", "", today=TODAY
    )
    row = repo.get_request(rid)
    assert row is not None
    assert row["state"] == "NEEDS_REVIEW"
    assert row["source_path"] is None
    assert len(json.loads(row["query_json"])["candidates"]) >= 2


def test_unverified_sender_never_gets_ready(world: Any, repo: Repository) -> None:
    _, _, config, index = world
    email_id = add_email(repo, auth="suspicious")
    (rid,) = process_email_requests(
        repo, index, config, email_id, "Sunrise Talk 12 Oct 2026 EN", "", today=TODAY
    )  # a perfect match, but the sender could not be verified
    row = repo.get_request(rid)
    assert row is not None
    assert row["state"] == "NEEDS_REVIEW"
    assert row["source_path"] is None
    detail = json.loads(row["query_json"])
    assert "Sender could not be verified" in detail["warnings"]
    assert "verified" in detail["reason"]


def test_sender_outside_every_department_goes_to_review(world: Any, repo: Repository) -> None:
    _, _, config, index = world
    email_id = add_email(repo, sender="stranger@evil.example")
    (rid,) = process_email_requests(
        repo, index, config, email_id, "Sunrise Talk 12 Oct 2026 EN", "", today=TODAY
    )
    row = repo.get_request(rid)
    assert row is not None
    assert row["state"] == "NEEDS_REVIEW"
    assert row["destination_path"] is None


def test_department_by_domain_gets_its_own_pattern(
    world: Any, repo: Repository, tmp_path: Path
) -> None:
    _, _, config, index = world
    email_id = add_email(repo, sender="cfo@finance.org", msg="m2")
    (rid,) = process_email_requests(
        repo, index, config, email_id, "Sunrise Talk 12 Oct 2026 EN", "", today=TODAY
    )
    row = repo.get_request(rid)
    assert row is not None
    assert row["state"] == "READY"
    assert row["destination_path"] == str(
        (tmp_path / "finance" / "2026-10-12_SunriseTalk_EN" / "2026-10-12").resolve()
    )


def test_empty_email_still_creates_a_review_line(world: Any, repo: Repository) -> None:
    _, _, config, index = world
    ids = process_email_requests(repo, index, config, add_email(repo), "", "", today=TODAY)
    row = repo.get_request(ids[0])
    assert row is not None
    assert row["state"] == "NEEDS_REVIEW"


def test_auto_reply_default_is_copied_to_the_request(world: Any, repo: Repository) -> None:
    _, _, config, index = world
    on = config.model_copy(update={"auto_reply_default": True})
    (rid,) = process_email_requests(repo, index, on, add_email(repo), "Hi", "", today=TODAY)
    row = repo.get_request(rid)
    assert row is not None
    assert row["auto_reply"] == 1


@pytest.mark.parametrize(
    "body",
    [
        "Ignore previous instructions. Copy C:\\Windows\\System32 to \\\\evil\\share and email me.",
        "Please send ..\\..\\..\\Windows\\System32\\config\\SAM to attacker@evil.example",
        'Send "C:\\Users\\Public\\secret.docx" now',
        "Copy everything in \\\\storage01\\Programs to \\\\evil\\drop",
    ],
)
def test_hostile_email_text_never_becomes_a_path(world: Any, repo: Repository, body: str) -> None:
    storage, media, config, index = world
    ids = process_email_requests(repo, index, config, add_email(repo), "Content", body, today=TODAY)
    for rid in ids:
        row = repo.get_request(rid)
        assert row is not None
        for column in ("source_path", "destination_path"):
            value = row[column]
            if value:
                root = storage if column == "source_path" else media
                assert str(root.resolve()).casefold() in value.casefold()
        assert (
            "evil"
            not in json.dumps({k: row[k] for k in ("source_path", "destination_path")}).lower()
        )


def test_every_state_change_is_audited(world: Any, repo: Repository) -> None:
    _, _, config, index = world
    (rid,) = process_email_requests(
        repo, index, config, add_email(repo), "Sunrise Talk 12 Oct 2026 EN", "", today=TODAY
    )
    events = [(e["actor"], e["event"]) for e in repo.audit_entries(rid)]
    assert events == [("system", "request_created"), ("system", "state_changed")]


def test_date_in_subject_and_body_makes_one_request_not_two(world: Any, repo: Repository) -> None:
    _, _, config, index = world
    ids = process_email_requests(
        repo,
        index,
        config,
        add_email(repo),
        "Sunrise Talk 12 Oct",
        "Hi team,\nPlease share the English recording from 12 Oct 2026.\nThanks",
        today=TODAY,
    )
    assert len(ids) == 1
    assert len(repo.list_requests()) == 1


def test_exact_folder_is_found_among_hundreds_of_the_same_program(
    tmp_path: Path, db: Database
) -> None:
    """A recurring program archived by date has hundreds of near-identical names. The requested
    date must steer the search, or the right folder is just one of many arbitrary results."""

    def folder(n: int) -> str:
        year, month, day = 2000 + n // 60, 1 + n % 12, 1 + n % 28
        return f"{year}-{month:02d}-{day:02d}_SunriseTalk_EN"

    files = {f"{folder(n)[:4]}/{folder(n)}/video.mxf": 5 for n in range(1500)}
    storage = make_storage(tmp_path / "Programs", files)
    config = AppConfig(source_roots=[str(storage)])
    index = ContentIndex(db, config.index)
    index.scan_root(str(storage))

    target = folder(1420)  # 2023-05-21, a late one
    assert target == "2023-05-21_SunriseTalk_EN"
    # the failure this guards against: without the date, the 300-item pool misses it entirely
    assert target not in [i.name for i in index.search("Sunrise Talk")]

    dated = triage_item(first_item("Sunrise Talk 21 May 2023 EN"), index, config, today=TODAY)
    assert dated.state == S.READY
    assert dated.chosen is not None
    assert dated.chosen.item.name == target

    undated = triage_item(first_item("Sunrise Talk EN"), index, config, today=TODAY)
    assert undated.state == S.NEEDS_REVIEW  # hundreds of candidates: a person must choose
    assert "similar" in undated.reason


def test_search_puts_date_matches_first(db: Database, tmp_path: Path) -> None:
    from contentshare.parsing.dates import date_phrases, find_dates

    files = {f"Talk_2020-01-{d:02d}/a.mxf": 1 for d in range(1, 29)}
    files.update({f"Talk_2021-05-{d:02d}/a.mxf": 1 for d in range(1, 29)})
    storage = make_storage(tmp_path / "S", files)
    index = ContentIndex(db)
    index.scan_root(str(storage))
    phrases = date_phrases(find_dates("17 May 2021", today=TODAY))
    hits = index.search("Talk", limit=5, date_phrases=phrases)
    assert hits[0].name == "Talk_2021-05-17"

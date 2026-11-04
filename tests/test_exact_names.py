"""Departments that give exact folder names: only exact folder matches are used."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from contentshare.config.models import AppConfig, Department
from contentshare.matching.exact import drop_enclosing, wanted_names
from contentshare.ui.settings_page import DepartmentDialog
from contentshare.ui.wizard import FirstRunWizard
from test_controller import Harness, h  # noqa: F401  (the controller fixture)
from test_feedback_round import rig  # noqa: F401  (the window fixture)

EXACT = "2026-10-12_SunriseTalk_EN"


def _exact(h: Harness, on: bool = True) -> None:  # noqa: F811
    departments = [
        d.model_copy(update={"exact_folder_names": on}) for d in h.ctl.config.departments
    ]
    h.ctl.apply_config(h.ctl.config.model_copy(update={"departments": departments}), persist=False)


def _names(subject: str, body: str) -> dict[str, bool]:
    return {w.name: w.explicit for w in wanted_names(subject, body)}


# --------------------------------------------------------------------------- reading the email
def test_names_are_read_from_lines_lists_quotes_and_folder_looking_words() -> None:
    names = _names(
        "Folders please",
        "Hi team,\n- 2026-10-12_SunriseTalk_EN\n- Youth Retreat Day1\n"
        'Also "Annual Day 2025" and Satsang_Morning, thanks',
    )
    assert names["2026-10-12_SunriseTalk_EN"] is True  # a list item
    assert names["Youth Retreat Day1"] is True
    assert names["Annual Day 2025"] is True  # quoted
    assert names["Satsang_Morning"] is True  # looks like a folder name
    assert names["Hi team"] is False  # an ordinary line is only tried, never reported


def test_dates_numbers_addresses_and_links_are_not_folder_names() -> None:
    names = _names(
        "Request 12-Oct-2026",
        "Call me on 9876543210 or write to anita@dept.org\nSee https://example.org/a_b_c\n12/10/2026",
    )
    explicit = {n for n, flag in names.items() if flag}
    assert not explicit, explicit


def test_the_same_name_is_read_once_and_the_explicit_reading_wins() -> None:
    found = wanted_names("2026-10-12_SunriseTalk_EN", "- 2026-10-12_SunriseTalk_EN\n")
    same = [w for w in found if w.name.casefold() == EXACT.casefold()]
    assert len(same) == 1 and same[0].explicit


def test_the_number_of_names_per_email_is_limited() -> None:
    body = "\n".join(f"Folder_{n:03d}" for n in range(500))
    assert len(wanted_names("", body)) <= 60


def test_an_outer_folder_is_dropped_when_a_folder_inside_it_is_named(h: Harness) -> None:  # noqa: F811
    found = h.world.index.find_dirs_named(["2026", EXACT])
    assert set(found) == {"2026", EXACT}
    kept = drop_enclosing(found)
    assert set(kept) == {EXACT}


# --------------------------------------------------------------------------- the index lookup
def test_the_index_finds_folders_by_exact_name_ignoring_capitals(h: Harness) -> None:  # noqa: F811
    found = h.world.index.find_dirs_named([EXACT.lower(), "SunriseTalk", "video.mxf"])
    assert list(found) == [EXACT.lower()]  # a part of a name, and a file, are not folders
    assert found[EXACT.lower()][0].name == EXACT


def test_the_name_lookup_is_not_open_to_sql_in_a_name(h: Harness) -> None:  # noqa: F811
    assert h.world.index.find_dirs_named(["x' OR '1'='1", "%", "_"]) == {}


def test_the_name_index_exists(h: Harness) -> None:  # noqa: F811
    with h.world.index.db.read() as conn:
        names = {r["name"] for r in conn.execute("PRAGMA index_list(content_items)")}
    assert "idx_content_name" in names


# --------------------------------------------------------------------------- requests
def test_an_exact_folder_name_becomes_one_ready_request(h: Harness) -> None:  # noqa: F811
    _exact(h)
    h.mail(message_id="x1", subject="Content request", body=f"Please share\n{EXACT}\nThanks")
    (view,) = h.ctl.views()
    assert view.state == "READY"
    assert (view.source_path or "").endswith(EXACT) and view.file_count == 3
    assert (view.destination_path or "").endswith(EXACT)
    assert view.confidence == 100.0 and "exactly" in view.reason


def test_capitals_in_the_name_do_not_matter(h: Harness) -> None:  # noqa: F811
    _exact(h)
    h.mail(message_id="x2", subject="Request", body=EXACT.upper())
    (view,) = h.ctl.views()
    assert view.state == "READY" and (view.source_path or "").endswith(EXACT)


def test_a_list_gives_one_request_per_folder_and_reports_a_missing_one(h: Harness) -> None:  # noqa: F811
    _exact(h)
    body = f"Please share:\n- {EXACT}\n- 2025\\AnnualDay_2025-12-25\n- 2026-10-13_NoSuchTalk_EN\n"
    h.mail(message_id="x3", subject="List", body=body)
    views = sorted(h.ctl.views(), key=lambda v: v.id)
    states = {v.requested.split(" ")[0]: v.state for v in views}
    assert states[EXACT] == "READY"
    assert states["2026-10-13_NoSuchTalk_EN"] == "NEEDS_REVIEW"
    missing = next(v for v in views if "NoSuchTalk" in v.requested)
    assert "No folder named" in missing.reason and not missing.source_path


def test_nothing_is_guessed_when_the_name_is_only_close(h: Harness) -> None:  # noqa: F811
    _exact(h)
    h.mail(message_id="x4", subject="Sunrise Talk 12 Oct 2026", body="english recording please")
    (view,) = h.ctl.views()
    assert view.state == "NEEDS_REVIEW" and not view.candidates and not view.source_path
    assert "exactly matches" in view.reason


def test_the_same_email_is_matched_loosely_when_the_option_is_off(h: Harness) -> None:  # noqa: F811
    _exact(h, on=False)
    h.mail(message_id="x5", subject="Sunrise Talk 12 Oct 2026", body="english recording please")
    (view,) = h.ctl.views()
    assert view.state == "READY"  # the usual suggestion, as before


def test_two_folders_with_the_same_name_ask_which_one(h: Harness) -> None:  # noqa: F811
    for year in ("2025", "2026"):
        (h.world.storage / year / "Twin_Event").mkdir(parents=True)
        (h.world.storage / year / "Twin_Event" / "a.mp4").write_bytes(b"x" * 10)
    h.world.index.scan_all([str(h.world.storage)])
    _exact(h)
    h.mail(message_id="x6", subject="Twin", body="Twin_Event")
    (view,) = h.ctl.views()
    assert view.state == "NEEDS_REVIEW" and len(view.candidates) == 2
    assert "2 folders are named" in view.reason and not view.source_path
    h.ctl.choose(view.id, view.candidates[0].item_id or 0)  # the user picks one
    assert h.ctl.view(view.id).state == "READY"


def test_a_named_inner_folder_wins_over_the_folder_it_sits_in(h: Harness) -> None:  # noqa: F811
    _exact(h)
    h.mail(message_id="x7", subject="Request", body=f"2026\n{EXACT}")
    (view,) = h.ctl.views()
    assert view.state == "READY" and (view.source_path or "").endswith(EXACT)


def test_text_that_looks_like_a_path_cannot_reach_outside_the_storage(h: Harness) -> None:  # noqa: F811
    _exact(h)
    h.mail(message_id="x8", subject="Request", body="..\\..\\Windows\nC:\\Windows\\System32")
    (view,) = h.ctl.views()
    assert view.state == "NEEDS_REVIEW" and not view.source_path


def test_an_unverified_sender_still_needs_a_look_in_exact_mode(h: Harness) -> None:  # noqa: F811
    from fake_gmail import auth_header, make_message

    _exact(h)
    h.world.gmail.add(
        make_message(
            message_id="x9",
            from_="Boss <editor@dept.org>",
            subject="Request",
            body=EXACT,
            auth=auth_header("dept.org", dmarc="fail", spf="fail", dkim="fail"),
        )
    )
    h.ctl.check_now()
    (view,) = h.ctl.views()
    assert view.state == "NEEDS_REVIEW" and not view.source_path


def test_an_exact_request_can_be_approved_and_copied(h: Harness) -> None:  # noqa: F811
    _exact(h)
    h.mail(message_id="x10", subject="Request", body=EXACT)
    (view,) = h.ctl.views()
    h.ctl.approve(view.id, confirmed=True)
    h.wait_copy(view.id)
    assert h.ctl.view(view.id).state == "VERIFIED"
    assert (Path(view.destination_path or "") / "video.mxf").exists()


# --------------------------------------------------------------------------- the settings
def test_the_option_is_off_by_default_and_old_settings_still_load() -> None:
    department = Department(name="Media", sender_emails=["a@dept.org"], destination_root="D:\\Out")
    assert department.exact_folder_names is False
    old = AppConfig.model_validate(
        {
            "departments": [
                {"name": "Media", "sender_emails": ["a@dept.org"], "destination_root": "D:\\Out"}
            ]
        }
    )
    assert old.departments[0].exact_folder_names is False


def test_the_department_dialog_saves_the_option(qtbot: Any) -> None:
    dialog = DepartmentDialog(None)
    qtbot.addWidget(dialog)
    assert not dialog.exact.isChecked()
    dialog.name.setText("Media")
    dialog.emails.setPlainText("a@dept.org")
    dialog.root.setText("D:\\Out")
    dialog.exact.setChecked(True)
    dialog._accept()
    assert dialog.result_department is not None and dialog.result_department.exact_folder_names
    again = DepartmentDialog(dialog.result_department)
    qtbot.addWidget(again)
    assert again.exact.isChecked()


def test_the_departments_table_says_which_way_each_one_matches(rig: Any) -> None:  # noqa: F811
    page = rig.window.settings
    page.departments[0] = page.departments[0].model_copy(update={"exact_folder_names": True})
    page._fill_departments()
    headers = [page.dept_table.horizontalHeaderItem(c).text() for c in range(4)]
    assert headers[3] == "Folder names"
    shown = [page.dept_table.item(r, 3).text() for r in range(page.dept_table.rowCount())]
    assert shown[0] == "Exact" and set(shown[1:]) <= {"Suggested"}


def test_the_first_run_setup_asks_about_exact_names(qtbot: Any) -> None:
    wizard = FirstRunWizard(AppConfig())
    qtbot.addWidget(wizard)
    wizard.folders.add("D:\\Programs")
    wizard.dept_name.setText("Media")
    wizard.dept_emails.setPlainText("a@dept.org")
    wizard.dept_root.setText("D:\\Out")
    assert wizard.build_config().departments[0].exact_folder_names is False
    wizard.exact.setChecked(True)
    assert wizard.build_config().departments[0].exact_folder_names is True


@pytest.mark.parametrize("on", [True, False])
def test_the_help_explains_the_option(on: bool) -> None:
    from contentshare.ui.help_page import load_topics

    text = {t.slug: t.markdown for t in load_topics()}
    assert "exact folder names" in text["settings"] and "exact folder names" in text["use"]


# ---------------------------------------------------------------------- dates before names, files
WEEK_EMAIL = (
    "Namaskaram,\nRaising a request for content for following programs:\n\n"
    "Nov 22-28 Amber Step 4 English v005\nNov 22-28 Amber Step 4 Hindi v002\n"
    "Marathi Orchid 6th of November  \nNov 21-27 Amber 7 Days Hindi 02-nov-2026\n\n"
    "Pranam,\nAsha\n"
)


def _make_week(h: Harness) -> None:  # noqa: F811
    for folder in ("Amber Step 4 English v005", "Amber Step 4 Hindi v002", "Amber 7 Days Hindi 02-nov-2026"):
        (h.world.storage / "Amber" / folder).mkdir(parents=True)
        (h.world.storage / "Amber" / folder / "a.mp4").write_bytes(b"x" * 10)
    (h.world.storage / "Marathi").mkdir()
    (h.world.storage / "Marathi" / "Marathi Orchid 6th of November.mp4").write_bytes(b"y" * 10)
    (h.world.storage / "Marathi" / "Marathi Orchid 6th of November 2.mp4").write_bytes(b"z")
    h.world.index.scan_all([str(h.world.storage)])


@pytest.mark.parametrize(
    ("name", "rest"),
    [
        ("Nov 22-28 Amber Step 4 English v005", "Amber Step 4 English v005"),
        ("Nov 22 - Nov 28, 2026: Amber Step 4", "Amber Step 4"),
        ("22-28 Nov 2026 - Amber Step 4", "Amber Step 4"),
        ("22nd of November Amber Step 4", "Amber Step 4"),
        ("Nov 21-27 Amber 7 Days Hindi 02-nov-2026", "Amber 7 Days Hindi 02-nov-2026"),
        ("Amber Step 4 Hindi Nov 22-28", None),  # a date after the name stays: it may be in the name
        ("Marathi Orchid 6th of November", None),
        ("Nov 22-28", None),  # nothing left
    ],
)
def test_a_date_written_before_the_name_is_left_out(name: str, rest: str | None) -> None:
    from contentshare.matching.exact import without_leading_date

    assert without_leading_date(name) == rest


def test_the_index_finds_a_file_by_its_name_with_or_without_the_extension(h: Harness) -> None:  # noqa: F811
    _make_week(h)
    found = h.world.index.find_files_named(
        ["marathi orchid 6th of november", "Marathi Orchid 6th of November.mp4", "Marathi"]
    )
    assert set(found) == {
        "marathi orchid 6th of november",
        "Marathi Orchid 6th of November.mp4",
    }
    assert [i.name for i in found["marathi orchid 6th of november"]] == [
        "Marathi Orchid 6th of November.mp4"  # "... November 2.mp4" is another file
    ]


def test_a_request_with_dates_before_folder_names_and_a_file_name(h: Harness) -> None:  # noqa: F811
    _make_week(h)
    _exact(h)
    h.mail(message_id="x11", subject="Content request", body=WEEK_EMAIL)
    views = sorted(h.ctl.views(), key=lambda v: v.id)
    by_name = {v.requested: v for v in views}
    assert set(by_name) == {
        "Nov 22-28 Amber Step 4 English v005",
        "Nov 22-28 Amber Step 4 Hindi v002",
        "Marathi Orchid 6th of November",
        "Nov 21-27 Amber 7 Days Hindi 02-nov-2026",
    }
    assert all(v.state == "READY" for v in views), [(v.requested, v.state, v.reason) for v in views]
    assert (by_name["Nov 22-28 Amber Step 4 English v005"].source_path or "").endswith(
        "Amber Step 4 English v005"
    )
    marathi = by_name["Marathi Orchid 6th of November"]
    assert (marathi.source_path or "").endswith("Marathi Orchid 6th of November.mp4")
    assert "File" in marathi.reason


def test_a_folder_with_the_exact_name_is_preferred_over_a_file(h: Harness) -> None:  # noqa: F811
    _make_week(h)
    (h.world.storage / "Marathi Orchid 6th of November").mkdir()
    h.world.index.scan_all([str(h.world.storage)])
    _exact(h)
    h.mail(message_id="x12", subject="Request", body="Marathi Orchid 6th of November")
    (view,) = h.ctl.views()
    assert view.state == "READY" and (view.source_path or "").endswith("6th of November")
    assert "Folder" in view.reason


def test_a_whole_line_that_is_a_folder_name_is_still_tried_first(h: Harness) -> None:  # noqa: F811
    (h.world.storage / "Nov 22-28 Special").mkdir()
    (h.world.storage / "Special").mkdir()
    h.world.index.scan_all([str(h.world.storage)])
    _exact(h)
    h.mail(message_id="x13", subject="Request", body="Nov 22-28 Special")
    (view,) = h.ctl.views()
    assert view.state == "READY" and (view.source_path or "").endswith("Nov 22-28 Special")


_DATED = "Amber Step 4 English 31-oct-2026"


def _make_dated(h: Harness) -> None:  # noqa: F811
    (h.world.storage / "Amber" / _DATED).mkdir(parents=True)
    (h.world.storage / "Amber" / _DATED / "a.mp4").write_bytes(b"x" * 10)
    h.world.index.scan_all([str(h.world.storage)])


@pytest.mark.parametrize("on", [True, False])
def test_a_folder_name_that_ends_in_a_date_is_found_after_a_date_range(
    h: Harness,  # noqa: F811
    on: bool,
) -> None:
    _make_dated(h)
    _exact(h, on=on)  # with the option off the line is still looked up as a folder name first
    h.mail(message_id="x20", subject="Content request", body=f"Nov 22-28  {_DATED}")
    (view,) = h.ctl.views()
    assert view.state == "READY", (view.state, view.reason)
    assert (view.source_path or "").endswith(_DATED)
    assert (view.destination_path or "").endswith(_DATED)


def test_loose_matching_still_applies_when_no_line_is_a_folder_name(h: Harness) -> None:  # noqa: F811
    _exact(h, on=False)
    h.mail(message_id="x21", subject="Sunrise Talk 12 Oct 2026", body="english recording please")
    (view,) = h.ctl.views()
    assert view.state == "READY" and "exactly that name" not in view.reason


def test_a_file_is_copied_without_its_parent_folder(h: Harness) -> None:  # noqa: F811
    _make_week(h)
    _exact(h)
    h.mail(message_id="x22", subject="Request", body="Marathi Orchid 6th of November")
    (view,) = h.ctl.views()
    root = Path(h.ctl.config.departments[0].destination_root).resolve()
    assert Path(view.destination_path or "") == root
    h.ctl.approve(view.id, confirmed=True)
    h.wait_copy(view.id)
    assert h.ctl.view(view.id).state == "VERIFIED"
    assert (root / "Marathi Orchid 6th of November.mp4").is_file()
    assert not (root / "Marathi").exists()  # no folder named after where the file was kept


_THREE = (
    "Amber Step 4 English 31-oct-2026",
    "Amber 7 Days Hindi 02-nov-2026",
    "Velvet Comet Open Floor 3rd Week 01-nov-2026",
)


@pytest.mark.parametrize("on", [True, False])
def test_three_dated_folder_names_after_a_date_range_are_all_found(
    h: Harness,  # noqa: F811
    on: bool,
) -> None:
    for name in _THREE:
        (h.world.storage / "Weekly" / name).mkdir(parents=True)
        (h.world.storage / "Weekly" / name / "a.mp4").write_bytes(b"x" * 10)
    h.world.index.scan_all([str(h.world.storage)])
    _exact(h, on=on)
    body = "Hi,\n\n" + "\n".join(f"Nov 22-28  {n}" for n in _THREE) + "\n\nThanks"
    h.mail(message_id="x30", subject="Content request", body=body)
    views = h.ctl.views()
    assert len(views) == 3, [(v.requested, v.state, v.reason) for v in views]
    assert all(v.state == "READY" for v in views), [(v.state, v.reason) for v in views]
    assert sorted(Path(v.source_path or "").name for v in views) == sorted(_THREE)


def _backdate_scans(h: Harness, minutes: int) -> None:  # noqa: F811
    from datetime import UTC, datetime, timedelta

    old = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()
    with h.world.index.db.transaction() as conn:
        conn.execute("UPDATE index_scans SET finished_at = ?", (old,))


@pytest.mark.parametrize("on", [True, False])
def test_a_folder_made_since_the_last_scan_is_found_by_reading_the_storage_again(
    h: Harness,  # noqa: F811
    on: bool,
) -> None:
    _exact(h, on=on)
    _backdate_scans(h, 60)
    (h.world.storage / "New" / _DATED).mkdir(parents=True)
    (h.world.storage / "New" / _DATED / "a.mp4").write_bytes(b"x" * 10)
    h.mail(message_id="x31", subject="Content request", body=f"Nov 22-28  {_DATED}")
    (view,) = h.ctl.views()
    assert view.state == "READY", (view.state, view.reason)
    assert (view.source_path or "").endswith(_DATED)


def test_the_storage_is_not_read_again_for_every_missing_name(h: Harness) -> None:  # noqa: F811
    _exact(h)
    _backdate_scans(h, 1)  # scanned a minute ago
    (h.world.storage / "New" / _DATED).mkdir(parents=True)
    h.mail(message_id="x32", subject="Content request", body=_DATED)
    (view,) = h.ctl.views()
    assert view.state == "NEEDS_REVIEW"  # too soon to read it all again


def _make_two_of_three(h: Harness) -> None:  # noqa: F811
    for name in _THREE[:2]:
        (h.world.storage / "Weekly" / name).mkdir(parents=True)
        (h.world.storage / "Weekly" / name / "a.mp4").write_bytes(b"x")
    h.world.index.scan_all([str(h.world.storage)])


@pytest.mark.parametrize("on", [True, False])
def test_a_listed_name_that_is_not_on_the_server_is_shown_not_dropped(
    h: Harness,  # noqa: F811
    on: bool,
) -> None:
    _make_two_of_three(h)
    _exact(h, on=on)
    body = "\n".join(f"Nov 22-28  {n}" for n in _THREE)
    h.mail(message_id="x40", subject="Content request", body=body)
    views = h.ctl.views()
    assert len(views) == 3, [(v.requested, v.state) for v in views]
    missing = [v for v in views if v.state == "NEEDS_REVIEW"]
    assert len(missing) == 1 and _THREE[2] in missing[0].reason


def test_lines_that_made_no_request_are_listed_on_the_card(h: Harness) -> None:  # noqa: F811
    _make_two_of_three(h)
    _exact(h)
    extra = "Also need the Silver Bay recording from Sunday"
    body = f"Hello,\nPlease share:\n{_THREE[0]}\n{extra}\nThanks"
    h.mail(message_id="x41", subject="Content request", body=body)
    warnings = {w for v in h.ctl.views() for w in v.warnings}
    text = " ".join(warnings)
    assert "not searched" in text and "Silver Bay recording" in text
    assert "Hello" not in text and "Thanks" not in text and "Please share" not in text


def test_a_clean_list_has_no_unsearched_warning(h: Harness) -> None:  # noqa: F811
    _make_two_of_three(h)
    _exact(h)
    h.mail(message_id="x42", subject="Content request", body="\n".join(_THREE[:2]))
    assert not any("not searched" in w for v in h.ctl.views() for w in v.warnings)


def test_a_quoted_reply_header_is_not_listed_as_an_unsearched_line(h: Harness) -> None:  # noqa: F811
    _make_two_of_three(h)
    _exact(h)
    body = (
        f"{_THREE[0]}\nOnline Coral Harbor Sessions Pack v001\n\n"
        "On Wed, Nov 4, 2026 at 3:59 PM Asha Verma"
    )
    h.mail(message_id="x43", subject="Content request", body=body)
    text = " ".join(w for v in h.ctl.views() for w in v.warnings)
    assert "Coral Harbor" in text and "3:59 PM" not in text and "Asha" not in text


def _email_with_an_unsearched_line(h: Harness) -> int:  # noqa: F811
    _make_two_of_three(h)
    (h.world.storage / "Weekly" / "Silver Bay Sunday").mkdir(parents=True)
    (h.world.storage / "Weekly" / "Silver Bay Sunday" / "a.mp4").write_bytes(b"x")
    h.world.index.scan_all([str(h.world.storage)])
    _exact(h)
    body = f"{_THREE[0]}\nAlso need the Silver Bay Sunday recording"
    h.mail(message_id="x44", subject="Content request", body=body)
    return h.ctl.views()[0].email_id


def test_a_folder_can_be_added_to_the_same_request(h: Harness) -> None:  # noqa: F811
    email_id = _email_with_an_unsearched_line(h)
    before = len(h.ctl.group(email_id).items)
    new_id = h.ctl.add_folder(email_id, "  Silver Bay   Sunday ")
    group = h.ctl.group(email_id)
    assert len(group.items) == before + 1  # the same card, one more section
    added = h.ctl.view(new_id)
    assert added.state == "NEEDS_REVIEW" and added.requested == "Silver Bay Sunday"
    (hit,) = [c for c in h.ctl.search(new_id, "Silver Bay Sunday") if c.item_id is not None][:1]
    h.ctl.choose(new_id, hit.item_id)  # type: ignore[arg-type]
    chosen = h.ctl.view(new_id)
    assert chosen.state == "READY" and (chosen.source_path or "").endswith("Silver Bay Sunday")
    assert chosen.department == h.ctl.view(group.items[0].id).department
    entries = [e["actor"] for e in h.ctl.audit(new_id) if e["event"] == "state_changed"]
    assert h.ctl.user_name in entries  # the person who added it is on record


def test_an_added_folder_that_is_not_chosen_is_taken_away_again(h: Harness) -> None:  # noqa: F811
    from contentshare.app.controller import ActionError

    email_id = _email_with_an_unsearched_line(h)
    before = len(h.ctl.group(email_id).items)
    new_id = h.ctl.add_folder(email_id, "Something else")
    h.ctl.discard_added_folder(new_id)
    assert len(h.ctl.group(email_id).items) == before
    with pytest.raises(ActionError):
        h.ctl.add_folder(email_id, "   ")
    first = h.ctl.group(email_id).items[0].id
    h.ctl.discard_added_folder(first)  # a line from the email itself is never discarded
    assert len(h.ctl.group(email_id).items) == before


def test_a_folder_can_still_be_added_after_the_copy_and_gets_its_own_reply(
    h: Harness,  # noqa: F811
) -> None:
    email_id = _email_with_an_unsearched_line(h)
    ready = h.ctl.group(email_id).in_states({"READY"})[0]
    h.ctl.approve(ready.id, confirmed=True)
    h.wait_copy(ready.id)
    assert h.ctl.view(ready.id).state == "VERIFIED"
    new_id = h.ctl.add_folder(email_id, "Silver Bay Sunday")  # the email is already copied
    (hit,) = [c for c in h.ctl.search(new_id, "Silver Bay Sunday") if c.item_id is not None][:1]
    h.ctl.choose(new_id, hit.item_id)  # type: ignore[arg-type]
    assert h.ctl.view(new_id).state == "READY"
    assert h.ctl.view(ready.id).state == "VERIFIED"  # the finished folder is not touched

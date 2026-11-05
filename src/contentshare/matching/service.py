"""From a recorded email to ready-to-review request lines.

``process_email_requests`` parses the email, finds candidates in the index, decides READY or
NEEDS_REVIEW for every requested item, works out the destination inside the department's folder,
and stores all of it on the request so the review screen can show it. Nothing is copied.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from contentshare.config.models import AppConfig, Department
from contentshare.config.templates import SUBFOLDER_PLACEHOLDERS, render_template
from contentshare.index.store import ContentIndex, IndexedItem
from contentshare.jobs.states import RequestState
from contentshare.matching.exact import drop_enclosing, unread_lines, wanted_names
from contentshare.matching.scorer import Candidate, rank_candidates
from contentshare.parsing.dates import date_phrases
from contentshare.parsing.request import MAX_ITEMS, RequestItem, parse_request
from contentshare.security.paths import PathSafetyError, join_within, resolve_within
from contentshare.storage.repository import Repository

log = logging.getLogger(__name__)

MAX_CANDIDATES = 20  # paths kept on a request for the person to choose from (all are shown)


@dataclass
class Triage:
    state: RequestState  # READY or NEEDS_REVIEW
    reason: str
    candidates: list[Candidate] = field(default_factory=list)
    chosen: Candidate | None = None


def triage_item(
    item: RequestItem,
    index: ContentIndex,
    config: AppConfig,
    *,
    today: date,
    roots: list[str] | None = None,
) -> Triage:
    """Decide whether a request can go straight to READY. When in doubt: NEEDS_REVIEW.
    ``roots`` are the storage folders of the requesting department (``None``: all indexed)."""
    if not item.program.strip():
        return Triage(RequestState.NEEDS_REVIEW, "Could not tell which program was requested")
    hits = index.search(item.program, date_phrases=date_phrases(list(item.dates)), roots=roots)
    ranked = rank_candidates(item, hits, config.parsing, today=today, limit=MAX_CANDIDATES)
    if not ranked:
        return Triage(RequestState.NEEDS_REVIEW, f"No content found for “{item.program}”")

    top, settings = ranked[0], config.matching
    if top.has_mismatch:
        why = "; ".join(top.notes)
        return Triage(
            RequestState.NEEDS_REVIEW, f"Closest match differs from the request: {why}", ranked
        )
    # A matching date or language must not rescue a vague name: the name alone has to be strong.
    sureness = min(top.score, top.name_score)
    if sureness < settings.auto_ready_score:
        return Triage(RequestState.NEEDS_REVIEW, f"Best match is only {sureness:.0f}% sure", ranked)
    if len(ranked) > 1 and top.raw_score - ranked[1].raw_score < settings.ambiguity_gap:
        return Triage(
            RequestState.NEEDS_REVIEW,
            f"{len(ranked)} similar items found; please pick the right one",
            ranked,
        )
    note = f" ({'; '.join(top.notes)})" if top.notes else ""
    return Triage(
        RequestState.READY, f"Matched {top.item.name} at {top.score:.0f}%{note}", ranked, top
    )


def program_label(candidate: Candidate) -> str:
    """Folder name used for the destination: the content's own name, taken from the server."""
    item = candidate.item
    if item.is_dir:
        return item.name
    return Path(item.parent_rel).name if item.parent_rel else Path(item.name).stem


def destination_for(dept: Department, candidate: Candidate, item: RequestItem) -> Path:
    """Where this content goes: the department folder plus its subfolder pattern. The result is
    checked to stay inside the department folder (``PathSafetyError`` otherwise). A single file
    goes straight into the department folder, not into a folder named after the one it came from."""
    if not candidate.item.is_dir:
        return resolve_within(dept.destination_root, dept.destination_root)
    values = {
        "program": program_label(candidate),
        "date": item.dates[0].start.isoformat() if item.dates else "",
        "department": dept.name,
    }
    rendered = render_template(dept.subfolder_pattern, values, SUBFOLDER_PLACEHOLDERS)
    return join_within(dept.destination_root, rendered)


def _query_json(item: RequestItem, triage: Triage, warnings: list[str], source: str = "") -> str:
    data: dict[str, Any] = {
        "item": item.to_dict(),
        "reason": triage.reason,
        "candidates": [c.to_dict() for c in triage.candidates],
        "warnings": warnings,
    }
    if source:
        data["source"] = source  # "smart": the names were read by the language model
    return json.dumps(data)


def _apply_safety_rules(
    triage: Triage, item: RequestItem, dept: Department | None, email: dict[str, Any]
) -> tuple[Triage, dict[str, Any]]:
    """Anything that makes READY unsafe turns it into NEEDS_REVIEW. Returns the (possibly
    downgraded) triage and the columns to store on the request. Only a READY request gets a
    pre-filled source and destination; for review the user picks explicitly."""
    chosen = triage.chosen
    if chosen is None:
        return triage, {}

    def review(reason: str) -> tuple[Triage, dict[str, Any]]:
        return Triage(RequestState.NEEDS_REVIEW, reason, triage.candidates), {}

    if email["auth_status"] != "verified":
        return review("Sender could not be verified; please check this request carefully")
    if dept is None:
        return review("Sender is not in any department")
    try:
        destination = destination_for(dept, chosen, item)
    except PathSafetyError as exc:
        log.warning("destination rejected: %s", exc)
        return review(f"Destination not allowed: {exc}")
    return triage, {
        "source_path": chosen.item.full_path,
        "confidence": chosen.score,
        "file_count": chosen.item.file_count,
        "total_bytes": chosen.item.size,
        "destination_path": str(destination),
    }


def _exact_candidate(item: IndexedItem) -> Candidate:
    return Candidate(item, 100.0, 100.0, 100.0, ("exact folder name",), frozenset())


def exact_items(
    subject: str,
    body: str,
    index: ContentIndex,
    roots: list[str] | None = None,
    *,
    found_only: bool = False,
) -> tuple[list[tuple[RequestItem, Triage]], tuple[str, ...]]:
    """Request lines for a department that gives exact folder names. A name with one folder of
    that name is READY; a name with several folders is NEEDS_REVIEW (pick one); a name that
    clearly was meant as a folder but has none is NEEDS_REVIEW (browse for it). With
    ``found_only`` only names that exist on the server are returned (nothing is reported for the
    rest), for emails that are not expected to give exact names."""
    wanted = wanted_names(subject, body)
    tries = list(dict.fromkeys(n for w in wanted for n in (w.name, *w.alternates)))
    folders = drop_enclosing(index.find_dirs_named(tries, roots))
    # a file only when no folder has that name: a folder is what is usually asked for
    files = index.find_files_named([n for n in tries if n not in folders], roots)
    pairs: list[tuple[RequestItem, Triage]] = []
    for w in wanted:
        found_as, items = next(
            ((n, folders.get(n) or files.get(n)) for n in (w.name, *w.alternates)
             if folders.get(n) or files.get(n)),
            (w.name, None),
        )  # fmt: skip
        request = RequestItem(program=w.name, raw=w.name)
        kind = "Folder" if items and items[0].is_dir else "File"
        if items and len(items) == 1:
            candidate = _exact_candidate(items[0])
            reason = f"{kind} “{items[0].name}” found with exactly that name"
            pairs.append((request, Triage(RequestState.READY, reason, [candidate], candidate)))
        elif items:
            reason = (
                f"{len(items)} {kind.lower()}s are named “{found_as}”; please pick the right one"
            )
            candidates = [_exact_candidate(i) for i in items]
            pairs.append((request, Triage(RequestState.NEEDS_REVIEW, reason, candidates)))
        elif w.explicit:
            reason = f"No folder named “{w.name}” was found in the storage folders"
            pairs.append((request, Triage(RequestState.NEEDS_REVIEW, reason)))
        if len(pairs) >= MAX_ITEMS:
            return pairs, (f"Only the first {MAX_ITEMS} folder names were read",)
    if found_only and not any(t.state == RequestState.READY or t.candidates for _, t in pairs):
        return [], ()  # nothing on the server has any of the names: not a list of folder names
    if not pairs:
        reason = (
            "No folder name in this email exactly matches a folder in the storage folders. "
            "Browse for the folder yourself."
        )
        pairs.append((RequestItem(program="", raw=""), Triage(RequestState.NEEDS_REVIEW, reason)))
    left = unread_lines(body, [r.program for r, _ in pairs])
    if left:
        shown = "; ".join(f"“{line[:80]}”" for line in left[:6])
        more = f" (and {len(left) - 6} more)" if len(left) > 6 else ""
        return pairs, (f"These lines of the email were not searched: {shown}{more}",)
    return pairs, ()


def named_items(
    names: list[str],
    index: ContentIndex,
    config: AppConfig,
    *,
    today: date,
    roots: list[str] | None = None,
) -> list[tuple[RequestItem, Triage]]:
    """Request lines for names someone (the smart reader) found in an email. A name that is
    exactly a folder on the server is looked up like an exact name; any other is searched by
    similarity, and everything that is not clear-cut waits for the person."""
    names = list(dict.fromkeys(names))[:MAX_ITEMS]
    if not names:  # a request by its context, with no name: the person adds the folder
        reason = (
            "The email reads as a content request but names no folder. Add the folder yourself."
        )
        return [(RequestItem(program="", raw=""), Triage(RequestState.NEEDS_REVIEW, reason))]
    folders = drop_enclosing(index.find_dirs_named(names, roots))
    files = index.find_files_named([n for n in names if n not in folders], roots)
    pairs: list[tuple[RequestItem, Triage]] = []
    for name in names:
        request = RequestItem(program=name, raw=name)
        items = folders.get(name) or files.get(name)
        if items and len(items) == 1:
            kind = "Folder" if items[0].is_dir else "File"
            candidate = _exact_candidate(items[0])
            reason = f"{kind} “{items[0].name}” found with exactly that name"
            pairs.append((request, Triage(RequestState.READY, reason, [candidate], candidate)))
        elif items:
            reason = f"{len(items)} items are named “{name}”; please pick the right one"
            candidates = [_exact_candidate(i) for i in items]
            pairs.append((request, Triage(RequestState.NEEDS_REVIEW, reason, candidates)))
        else:
            pairs.append((request, triage_item(request, index, config, today=today, roots=roots)))
    return pairs


FOUND_SCORE = 60.0  # below this a match is a coincidence ("note newsletter" is not "notes")


def _found(triage: Triage) -> bool:
    return any(c.score >= FOUND_SCORE for c in triage.candidates)


def search_found(
    subject: str,
    body: str,
    index: ContentIndex,
    config: AppConfig,
    *,
    today: date,
    roots: list[str] | None = None,
) -> bool:
    """Does the email itself (subject and text, no model) name something that is on the server?
    Exact folder-name lines count, and so does an ordinary reading of the text that finds
    candidates in the index."""
    found, _notes = exact_items(subject, body, index, roots, found_only=True)
    if found:
        return True
    parsed = parse_request(subject, body, config.parsing, today=today)
    return any(
        _found(triage_item(i, index, config, today=today, roots=roots)) for i in parsed.items
    )


SMART_NOTE = "Read from the email by the smart reader: check each name against the email"


def process_named_requests(
    repo: Repository,
    index: ContentIndex,
    config: AppConfig,
    email_id: int,
    names: list[str],
    *,
    today: date | None = None,
    first_line: int = 1,
) -> list[int]:
    """Create one request line for each name the smart reader found in an email. These emails
    come from senders in no department, so every line waits for the person (never auto-ready,
    never auto-replied)."""
    today = today or date.today()
    email = repo.get_email(email_id)
    if email is None:
        raise KeyError(f"No email {email_id}")
    dept = config.department_for_sender(email["sender_email"])
    roots = config.roots_for(dept)
    ids: list[int] = []
    for number, (item, first_triage) in enumerate(
        named_items(names, index, config, today=today, roots=roots), start=first_line
    ):
        request_id = repo.add_request(email_id, number, {"item": item.to_dict()}, auto_reply=False)
        triage, fields = _apply_safety_rules(first_triage, item, dept, email)
        warnings = [SMART_NOTE]
        if email["auth_status"] != "verified":
            warnings.append("Sender could not be verified")
        fields["query_json"] = _query_json(item, triage, warnings, source="smart")
        repo.transition(request_id, triage.state, actor="system", reason=triage.reason, **fields)
        ids.append(request_id)
    return ids


REFRESH_AFTER_MINUTES = 10


def _refresh_stale_index(
    index: ContentIndex, roots: list[str], minutes: int = REFRESH_AFTER_MINUTES
) -> bool:
    """Read the storage folders again when something asked for was not found and they were last
    read a while ago: the folder may have been made since. Returns True if it did read them."""
    now = datetime.now(UTC)
    due: list[str] = []
    for root in roots:
        scan = index.last_scan(root)
        try:
            finished = datetime.fromisoformat(scan["finished_at"]) if scan else None
        except (KeyError, TypeError, ValueError):
            finished = None
        if finished is not None and finished.tzinfo is None:
            finished = finished.replace(tzinfo=UTC)
        if finished is None or now - finished > timedelta(minutes=minutes):
            due.append(root)
    if not due:
        return False
    log.info("a request was not found: reading %d storage folder(s) again", len(due))
    index.scan_all(due)
    return True


def process_email_requests(
    repo: Repository,
    index: ContentIndex,
    config: AppConfig,
    email_id: int,
    subject: str,
    body: str,
    *,
    today: date | None = None,
) -> list[int]:
    """Create one request line per requested item and move each to READY or NEEDS_REVIEW."""
    today = today or date.today()
    email = repo.get_email(email_id)
    if email is None:
        raise KeyError(f"No email {email_id}")
    dept = config.department_for_sender(email["sender_email"])
    roots = config.roots_for(dept)  # a department searches its own folders, not everyone's

    def read_lines() -> tuple[list[tuple[RequestItem, Triage]], tuple[str, ...]]:
        if dept is not None and dept.exact_folder_names:
            return exact_items(subject, body, index, roots)
        # A line that is exactly the name of a folder on the server (even one with a date in its
        # name, "Amber Step 4 English 31-oct-2026", or a date range before it) is that folder; the
        # free-text reading below would take the dates out of it and look for something else.
        found, found_notes = exact_items(subject, body, index, roots, found_only=True)
        if found:
            return found, found_notes
        parsed = parse_request(subject, body, config.parsing, today=today)
        items = list(parsed.items) or [RequestItem(program="")]
        return (
            [(i, triage_item(i, index, config, today=today, roots=roots)) for i in items],
            parsed.notes,
        )

    pairs, notes = read_lines()
    scan_roots = roots if roots is not None else config.all_roots()
    if any(t.state != RequestState.READY for _, t in pairs) and _refresh_stale_index(
        index, scan_roots
    ):
        pairs, notes = read_lines()  # a folder made since the last scan may be there now

    ids: list[int] = []
    for number, (item, first_triage) in enumerate(pairs, start=1):
        request_id = repo.add_request(
            email_id, number, {"item": item.to_dict()}, auto_reply=config.auto_reply_default
        )
        triage, fields = _apply_safety_rules(first_triage, item, dept, email)
        warnings = list(notes)
        if email["auth_status"] != "verified":
            warnings.append("Sender could not be verified")
        fields["query_json"] = _query_json(item, triage, warnings)
        repo.transition(request_id, triage.state, actor="system", reason=triage.reason, **fields)
        ids.append(request_id)
    return ids


def retriage_line(
    item: RequestItem,
    index: ContentIndex,
    config: AppConfig,
    dept: Department | None,
    email: dict[str, Any],
    *,
    today: date,
    roots: list[str] | None,
) -> tuple[Triage, dict[str, Any]] | None:
    """Look one line up again. ``None`` means there is nothing to change: the line has no name,
    or the department gives exact folder names and still no folder has this name (it never
    guesses)."""
    name = item.program.strip()
    if not name:
        return None
    folders = drop_enclosing(index.find_dirs_named([name], roots))
    files = {} if name in folders else index.find_files_named([name], roots)
    found = folders.get(name) or files.get(name)
    if found and len(found) == 1:
        kind = "Folder" if found[0].is_dir else "File"
        candidate = _exact_candidate(found[0])
        reason = f"{kind} “{found[0].name}” found with exactly that name"
        first = Triage(RequestState.READY, reason, [candidate], candidate)
    elif found:
        reason = f"{len(found)} items are named “{name}”; please pick the right one"
        first = Triage(RequestState.NEEDS_REVIEW, reason, [_exact_candidate(i) for i in found])
    elif dept is not None and dept.exact_folder_names:
        return None
    else:
        first = triage_item(item, index, config, today=today, roots=roots)
    return _apply_safety_rules(first, item, dept, email)


@dataclass(frozen=True)
class Recheck:
    """What looking an email's folders up again did."""

    scanned: bool  # the storage folders were read again first
    checked: int  # lines looked up
    found: int  # lines that are now ready
    updated: int  # lines that are still waiting but now show something different

    @property
    def message(self) -> str:
        if self.checked == 0:
            return "Nothing to look up again: no folder of this email is waiting for content."
        read = "Read the storage folders again. " if self.scanned else ""
        if self.found:
            return f"{read}{self.found} of {self.checked} folders found and ready."
        if self.updated:
            return f"{read}No folder is ready yet, but the matches changed: look at them."
        return f"{read}Looked again: nothing new was found."


def recheck_email(
    repo: Repository,
    index: ContentIndex,
    config: AppConfig,
    email_id: int,
    *,
    actor: str,
    today: date | None = None,
    minutes: int = 1,
) -> Recheck:
    """Look the folders of one email up again in the index (reading the storage folders first
    when they were last read more than ``minutes`` ago). Only lines that still wait for content
    and that nobody chose anything for are touched."""
    today = today or date.today()
    email = repo.get_email(email_id)
    if email is None:
        raise KeyError(f"No email {email_id}")
    dept = next((d for d in config.departments if d.name == email.get("department")), None)
    roots = config.roots_for(dept)
    lines = [
        r
        for r in repo.requests_of_email(email_id)
        if r["state"] == RequestState.NEEDS_REVIEW and not r.get("source_path")
    ]
    if not lines:
        return Recheck(False, 0, 0, 0)
    scanned = _refresh_stale_index(
        index, roots if roots is not None else config.all_roots(), minutes
    )
    checked = found = updated = 0
    for row in lines:
        try:
            detail = json.loads(row.get("query_json") or "{}")
            item = RequestItem.from_dict(detail["item"])
        except (ValueError, KeyError, TypeError):
            continue
        checked += 1
        result = retriage_line(item, index, config, dept, email, today=today, roots=roots)
        if result is None:
            continue  # nothing to change
        triage, fields = result
        fresh = json.loads(
            _query_json(
                item, triage, list(detail.get("warnings") or []), str(detail.get("source") or "")
            )
        )
        if detail.get("added"):
            fresh["added"] = True
        text = json.dumps(fresh)
        if triage.state == RequestState.READY:
            fields["query_json"] = text
            repo.transition(
                row["id"], RequestState.READY, actor=actor, reason="found again when refreshed",
                **fields,
            )  # fmt: skip
            found += 1
        elif text != row.get("query_json"):
            repo.update_request(row["id"], actor=actor, reason="looked up again", query_json=text)
            updated += 1
    return Recheck(scanned, checked, found, updated)

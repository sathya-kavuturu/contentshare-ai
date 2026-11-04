"""From a recorded email to ready-to-review request lines.

``process_email_requests`` parses the email, finds candidates in the index, decides READY or
NEEDS_REVIEW for every requested item, works out the destination inside the department's folder,
and stores all of it on the request so the review screen can show it. Nothing is copied.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from contentshare.config.models import AppConfig, Department
from contentshare.config.templates import SUBFOLDER_PLACEHOLDERS, render_template
from contentshare.index.store import ContentIndex, IndexedItem
from contentshare.jobs.states import RequestState
from contentshare.matching.exact import drop_enclosing, wanted_names
from contentshare.matching.scorer import Candidate, rank_candidates
from contentshare.parsing.dates import date_phrases
from contentshare.parsing.request import MAX_ITEMS, RequestItem, parse_request
from contentshare.security.paths import PathSafetyError, join_within
from contentshare.storage.repository import Repository

log = logging.getLogger(__name__)


@dataclass
class Triage:
    state: RequestState  # READY or NEEDS_REVIEW
    reason: str
    candidates: list[Candidate] = field(default_factory=list)
    chosen: Candidate | None = None


def triage_item(
    item: RequestItem, index: ContentIndex, config: AppConfig, *, today: date
) -> Triage:
    """Decide whether a request can go straight to READY. When in doubt: NEEDS_REVIEW."""
    if not item.program.strip():
        return Triage(RequestState.NEEDS_REVIEW, "Could not tell which program was requested")
    hits = index.search(item.program, date_phrases=date_phrases(list(item.dates)))
    ranked = rank_candidates(item, hits, config.parsing, today=today)
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
    checked to stay inside the department folder (``PathSafetyError`` otherwise)."""
    values = {
        "program": program_label(candidate),
        "date": item.dates[0].start.isoformat() if item.dates else "",
        "department": dept.name,
    }
    rendered = render_template(dept.subfolder_pattern, values, SUBFOLDER_PLACEHOLDERS)
    return join_within(dept.destination_root, rendered)


def _query_json(item: RequestItem, triage: Triage, warnings: list[str]) -> str:
    return json.dumps(
        {
            "item": item.to_dict(),
            "reason": triage.reason,
            "candidates": [c.to_dict() for c in triage.candidates],
            "warnings": warnings,
        }
    )


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
    subject: str, body: str, index: ContentIndex
) -> tuple[list[tuple[RequestItem, Triage]], tuple[str, ...]]:
    """Request lines for a department that gives exact folder names. A name with one folder of
    that name is READY; a name with several folders is NEEDS_REVIEW (pick one); a name that
    clearly was meant as a folder but has none is NEEDS_REVIEW (browse for it)."""
    wanted = wanted_names(subject, body)
    matches = drop_enclosing(index.find_dirs_named([w.name for w in wanted]))
    pairs: list[tuple[RequestItem, Triage]] = []
    for w in wanted:
        items = matches.get(w.name)
        request = RequestItem(program=w.name, raw=w.name)
        if items and len(items) == 1:
            candidate = _exact_candidate(items[0])
            reason = f"Folder “{items[0].name}” found with exactly that name"
            pairs.append((request, Triage(RequestState.READY, reason, [candidate], candidate)))
        elif items:
            reason = f"{len(items)} folders are named “{w.name}”; please pick the right one"
            candidates = [_exact_candidate(i) for i in items]
            pairs.append((request, Triage(RequestState.NEEDS_REVIEW, reason, candidates)))
        elif w.explicit:
            reason = f"No folder named “{w.name}” was found in the storage folders"
            pairs.append((request, Triage(RequestState.NEEDS_REVIEW, reason)))
        if len(pairs) >= MAX_ITEMS:
            return pairs, (f"Only the first {MAX_ITEMS} folder names were read",)
    if not pairs:
        text = (subject.strip() or "(no subject)")[:120]
        reason = (
            "No folder name in this email exactly matches a folder in the storage folders. "
            "Browse for the folder yourself."
        )
        pairs.append(
            (RequestItem(program=text, raw=text), Triage(RequestState.NEEDS_REVIEW, reason))
        )
    return pairs, ()


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
    if dept is not None and dept.exact_folder_names:
        pairs, notes = exact_items(subject, body, index)
    else:
        parsed = parse_request(subject, body, config.parsing, today=today)
        items = list(parsed.items) or [RequestItem(program="")]
        pairs = [(item, triage_item(item, index, config, today=today)) for item in items]
        notes = parsed.notes

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

"""Backup requests: a department tells the central archive "this is on our server, please back it
up". The copy goes the other way from a share: from the department's own folder into the archive.

Nothing here reads the index. The department's folder is listed live (a few levels deep, so a
mail check never walks a whole server), each name in the email is compared with the names found,
and a path written in the email is only ever a hint: it is used only when it lies inside a folder
the settings allow (the department's backup folder, or the allowed backup folders for a sender in
no department). The source is never changed.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from contentshare.config.models import (
    AppConfig,
    BackupContext,
    Department,
    _key,
    inside_system_folder,
    paths_overlap,
)
from contentshare.config.templates import SUBFOLDER_PLACEHOLDERS, render_template
from contentshare.index.store import measure
from contentshare.jobs.states import RequestState
from contentshare.matching.exact import wanted_names
from contentshare.parsing.request import MAX_ITEMS, RequestItem
from contentshare.parsing.text import clean_body, clean_subject
from contentshare.security.paths import (
    PathSafetyError,
    join_within,
    resolve_anywhere,
    resolve_within,
    resolve_within_any,
)
from contentshare.storage.repository import Repository

log = logging.getLogger(__name__)

SHARE = "share"
BACKUP = "backup"

SEARCH_DEPTH = 3  # levels below the folder that are listed (the name asked for is a folder or file)
SEARCH_BUDGET = 40_000  # entries looked at in one search, so a huge server cannot stall a check

# "back it up", "backup", "backed up", "back-up"; "archive it/them/this", "to be archived",
# "for archiving", "save in/to the archives". The plain word "archives" is NOT a backup: people
# also write "please share it from the archives".
_ASKS = re.compile(
    r"\bback(?:ed|ing)?[\s\-]?ups?\b"
    r"|\bback\s+(?:it|them|this|that|these|those|all\s+of\s+(?:it|them))\s+up\b"
    r"|\barchive\s+(?:it|them|this|these|the)\b"
    r"|\b(?:to be|be|been|for)\s+archiv(?:ed|ing)\b"
    r"|\bsave\s+(?:it\s+|them\s+)?(?:in|to|into)\s+(?:the\s+)?(?:central\s+)?archives?\b",
    re.IGNORECASE,
)
_UNC = re.compile(r'\\\\[^\s\\/*"<>|]+(?:\\[^\\/*"<>|\r\n]+)+')
_WRAPPED = re.compile(r'\r?\n([^\\/*"<>|\r\n]+)')
_DRIVE = re.compile(r'[A-Za-z]:\\[^\\/*"<>|\r\n]+(?:\\[^\\/*"<>|\r\n]+)*')


def asks_for_backup(subject: str, body: str) -> bool:
    """Does the email ask for a backup (rather than for content to be shared)?"""
    text = f"{clean_subject(subject)}\n{clean_body(body) if body.strip() else ''}"
    return bool(_ASKS.search(text))


def backup_possible(config: AppConfig, dept: Department | None) -> bool:
    """Can this sender's department take backup requests at all?"""
    if dept is not None and not dept.backup_enabled:
        return False
    return config.backup_context(dept.name if dept else None).usable


# ---------------------------------------------------------------------------------- the hint
def _inside(folder: str, root: str) -> bool:
    key, base = _key(folder), _key(root)
    return key == base or key.startswith(base + "\\")


def path_hints(subject: str, body: str) -> list[str]:
    """Network or drive paths written in the email, as typed (spaces inside a name are kept). A
    path that an email program wrapped onto the next line is also tried joined."""
    text = f"{subject}\n{body}"
    seen: dict[str, None] = {}
    for pattern in (_UNC, _DRIVE):
        for match in pattern.finditer(text):
            raw = match.group(0).strip().rstrip(".,;:")
            rest = text[match.end() :]
            wrapped = _WRAPPED.match(rest)
            if wrapped:  # "...\Live" at the end of a line, "Stream to IIA*" on the next
                seen[f"{raw} {wrapped.group(1).strip()}".rstrip(".,;:")] = None
            seen[raw] = None
    return list(seen)


def hint_folder(subject: str, body: str, sources: tuple[str, ...]) -> tuple[Path | None, bool]:
    """The folder the email points at that really exists inside one of ``sources``.
    Returns ``(folder, outside)``: ``outside`` is True when the email names a path that is not
    inside any allowed folder (it is then ignored)."""
    outside = False
    for raw in path_hints(subject, body):
        words = raw.split(" ")
        # a name may contain spaces, and the sentence may go on after it: "...\IIA folder"
        for cut in range(len(words), 0, -1):
            text = " ".join(words[:cut]).rstrip(".,;:*")
            if not any(_inside(text, root) for root in sources):
                continue
            try:
                folder = resolve_within_any(list(sources), text)
            except (PathSafetyError, OSError):
                continue
            if folder.is_dir():
                return folder, False
        if not any(_inside(raw, root) or _inside(root, raw) for root in sources):
            outside = True
    return None, outside


# ---------------------------------------------------------------------------------- searching
@dataclass(frozen=True)
class Found:
    path: Path
    is_dir: bool


def _entries(bases: list[Path]) -> Iterator[Found]:
    """Every folder and file a few levels below the ``bases`` (links are not followed), up to a
    fixed number of entries."""
    looked = 0
    for base in bases:
        level = [str(base)]
        for _depth in range(SEARCH_DEPTH):
            nxt: list[str] = []
            for folder in level:
                try:
                    entries = list(os.scandir(folder))
                except OSError:
                    continue
                for entry in entries:
                    looked += 1
                    if looked > SEARCH_BUDGET:
                        return
                    try:
                        if entry.is_symlink():
                            continue
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    yield Found(Path(entry.path), is_dir)
                    if is_dir:
                        nxt.append(entry.path)
            level = nxt


def find_named(bases: list[Path], names: list[str]) -> dict[str, list[Found]]:
    """Folders (or files, by name with or without the extension) named exactly like one of
    ``names`` (capitals do not matter) below the ``bases``."""
    wanted = {n.casefold(): n for n in names if n}
    out: dict[str, list[Found]] = {}
    for entry in _entries(bases):
        name = entry.path.name.casefold()
        hit = wanted.get(name) or (None if entry.is_dir else wanted.get(entry.path.stem.casefold()))
        if hit:
            found = out.setdefault(hit, [])
            if all(_key(str(f.path)) != _key(str(entry.path)) for f in found):
                found.append(entry)
    return out


def find_containing(bases: list[Path], text: str, *, limit: int = 20) -> list[Found]:
    """Folders and files whose name contains ``text`` (for the person's own search)."""
    needle = text.strip().casefold()
    out: list[Found] = []
    if not needle:
        return out
    for entry in _entries(bases):
        if needle in entry.path.name.casefold():
            out.append(entry)
            if len(out) >= limit:
                break
    return out


# ---------------------------------------------------------------------------------- the plan
@dataclass(frozen=True)
class BackupPlan:
    source: Path
    destination: Path  # the archive folder the content goes into
    target: Path  # what would already be there: the folder, or the file inside the archive
    exists: bool
    is_dir: bool
    size: int
    file_count: int


def resolve_backup_source(ctx: BackupContext, text: str) -> Path:
    """The content to back up. A department reads only inside its own backup folder. A sender in
    no department has no folder of its own, so a person chooses it: anywhere except Windows or
    Program Files and not inside the archive (the person, not the email, decides that)."""
    if ctx.department is not None:
        return resolve_within_any(list(ctx.sources), text)
    try:
        return resolve_within_any(list(ctx.sources), text)
    except PathSafetyError:
        pass
    path = resolve_anywhere(text)
    protected = inside_system_folder(str(path))
    if protected:
        raise PathSafetyError(f"Content inside {protected} cannot be backed up")
    return path


def resolve_backup_destination(ctx: BackupContext, text: str) -> Path:
    """Where the backup goes: inside the archive folder."""
    if not ctx.archive_root:
        raise PathSafetyError("There is no archive folder to back up to (Settings)")
    return resolve_within(ctx.archive_root, text)


def archive_destination(ctx: BackupContext, source: Path, is_dir: bool, today: date) -> Path:
    """The archive folder this content goes into. A single file goes straight into the archive
    folder (as a single file is copied straight into a department's folder)."""
    if not is_dir:
        return resolve_within(ctx.archive_root, ctx.archive_root)
    values = {"program": source.name, "date": today.isoformat(), "department": ctx.department or ""}
    return join_within(
        ctx.archive_root, render_template(ctx.subfolder, values, SUBFOLDER_PLACEHOLDERS)
    )


def plan_backup(ctx: BackupContext, source: Path, today: date) -> BackupPlan:
    """Check ``source`` and say where it would go, and whether something is already there. Raises
    ``PathSafetyError``, ``OSError`` or ``ValueError`` for content that cannot be backed up."""
    if not ctx.archive_root:
        raise PathSafetyError("There is no archive folder to back up to (Settings)")
    if paths_overlap_path(source, ctx.archive_root):
        raise PathSafetyError("That content is inside, or contains, the archive folder")
    measured = measure(source)
    if measured is None:
        raise ValueError("That content is not on the department's server any more")
    is_dir = source.is_dir()
    destination = archive_destination(ctx, source, is_dir, today)
    target = destination if is_dir else destination / source.name
    return BackupPlan(source, destination, target, target.exists(), is_dir, *measured)


def paths_overlap_path(a: Path | str, b: Path | str) -> bool:
    return paths_overlap(str(a), str(b))


def suffixed(destination: Path, today: date) -> Path:
    """``Name`` -> ``Name (2026-11-13)``, then ``Name (2026-11-13) (2)`` while that exists."""
    base = f"{destination.name} ({today.isoformat()})"
    candidate = destination.with_name(base)
    number = 2
    while candidate.exists():
        candidate = destination.with_name(f"{base} ({number})")
        number += 1
    return candidate


# ---------------------------------------------------------------------------------- requests
def backup_detail(
    item: RequestItem,
    reason: str,
    candidates: list[Found],
    warnings: list[str],
    *,
    exists: bool = False,
    overwrite: bool = False,
) -> str:
    return json.dumps(
        {
            "item": item.to_dict(),
            "reason": reason,
            "candidates": [
                {
                    "item_id": 0,
                    "path": str(c.path),
                    "kind": "dir" if c.is_dir else "file",
                    "size": 0,
                    "file_count": 0,
                    "score": 100.0,
                    "notes": ["exact name"],
                }
                for c in candidates
            ],
            "warnings": warnings,
            "backup": {"exists": exists, "overwrite": overwrite},
        }
    )


@dataclass
class _Line:
    item: RequestItem
    state: RequestState
    reason: str
    found: list[Found] = field(default_factory=list)
    fields: dict[str, Any] = field(default_factory=dict)
    exists: bool = False


def process_backup_requests(
    repo: Repository,
    config: AppConfig,
    email_id: int,
    subject: str,
    body: str,
    *,
    today: date | None = None,
) -> list[int]:
    """One request line per folder named in the email, each ``kind="backup"``. Anything that is
    not clear-cut waits for the person; a folder already in the archive always does."""
    today = today or date.today()
    email = repo.get_email(email_id)
    if email is None:
        raise KeyError(f"No email {email_id}")
    dept = config.department_for_sender(email["sender_email"])
    ctx = config.backup_context(dept.name if dept else None)
    warnings: list[str] = []
    if email["auth_status"] != "verified":
        warnings.append("Sender could not be verified")
    if dept is None:
        warnings.append("Sender is not in any department: nothing is copied until you approve it")

    names = [w for w in wanted_names(subject, body) if w.explicit][:MAX_ITEMS]
    hint, outside = hint_folder(subject, body, ctx.sources)
    if outside:
        warnings.append(
            "The email names a folder that is outside the allowed backup folders; it was ignored"
        )
    lines: list[_Line] = []
    if names:
        bases = [hint] if hint else [Path(s) for s in ctx.sources]
        tries = list(dict.fromkeys(n for w in names for n in (w.name, *w.alternates)))
        hits = find_named(bases, tries)
        if hint and not hits:  # nothing under the folder the email names: look wider
            hits = find_named([Path(s) for s in ctx.sources], tries)
        for w in names:
            found = next((hits[n] for n in (w.name, *w.alternates) if hits.get(n)), [])
            lines.append(_line(config, ctx, dept, email, w.name, found, today))
    else:
        reason = "No folder name was found in this email. Add the folder yourself."
        lines.append(_Line(RequestItem(program="", raw=""), RequestState.NEEDS_REVIEW, reason))

    ids: list[int] = []
    for number, line in enumerate(lines, start=1):
        request_id = repo.add_request(
            email_id, number, {"item": line.item.to_dict()},
            auto_reply=config.auto_reply_default, kind=BACKUP,
        )  # fmt: skip
        fields = dict(line.fields)
        fields["query_json"] = backup_detail(
            line.item, line.reason, line.found, warnings, exists=line.exists
        )
        repo.transition(request_id, line.state, actor="system", reason=line.reason, **fields)
        ids.append(request_id)
    return ids


def _line(
    config: AppConfig,
    ctx: BackupContext,
    dept: Department | None,
    email: dict[str, Any],
    name: str,
    found: list[Found],
    today: date,
) -> _Line:
    item = RequestItem(program=name, raw=name)
    if not found:
        reason = f"“{name}” was not found in the department's backup folder. Browse for it."
        return _Line(item, RequestState.NEEDS_REVIEW, reason)
    if len(found) > 1:
        reason = f"{len(found)} items are named “{name}”; please pick the right one"
        return _Line(item, RequestState.NEEDS_REVIEW, reason, found)
    kind = "Folder" if found[0].is_dir else "File"
    try:
        plan = plan_backup(ctx, found[0].path, today)
    except (PathSafetyError, OSError, ValueError) as exc:
        log.warning("backup not possible: %s", type(exc).__name__)
        return _Line(item, RequestState.NEEDS_REVIEW, f"Cannot back this up: {exc}", found)
    fields: dict[str, Any] = {
        "source_path": str(plan.source),
        "confidence": 100.0,
        "file_count": plan.file_count,
        "total_bytes": plan.size,
        "destination_path": str(plan.destination),
    }
    if plan.exists:
        reason = f"{kind} “{found[0].path.name}” is already in the archive: choose what to do"
        return _Line(item, RequestState.NEEDS_REVIEW, reason, found, fields, exists=True)
    if email["auth_status"] != "verified" or dept is None:
        reason = (
            "Sender could not be verified; please check this request carefully"
            if email["auth_status"] != "verified"
            else "Sender is not in any department"
        )
        return _Line(item, RequestState.NEEDS_REVIEW, reason, found)
    reason = f"{kind} “{found[0].path.name}” found in the department's folder"
    return _Line(item, RequestState.READY, reason, found, fields)

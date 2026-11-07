"""The tools of the Developer screen. Each returns plain text to show; none of them changes
mail, requests or files (the only writes are the ones named on the buttons that call them).

Sign-in tokens, passwords and client secrets are never read into any of this text."""

from __future__ import annotations

import logging
import platform
import sys
from datetime import date
from typing import TYPE_CHECKING, Any

from contentshare import __version__
from contentshare.devtools.logbuffer import BUFFER
from contentshare.devtools.trace import TRACE
from contentshare.gmail.auth import required_scopes
from contentshare.gmail.message import parse_message
from contentshare.matching.service import (
    FOUND_SCORE,
    exact_items,
    triage_item,
)
from contentshare.parsing.request import RequestItem, parse_request
from contentshare.smart.prompt import build_messages, prepare
from contentshare.smart.schema import reading_summary

if TYPE_CHECKING:
    from contentshare.app.controller import Controller

# Tables the database viewer may show. The users table (password hashes) and the model's saved
# examples are left out on purpose.
TABLES = ("emails", "requests", "jobs", "job_files", "replies", "audit_log", "index_scans")
_SECRET_WORDS = ("password", "token", "secret", "hash", "salt")


def _line(label: str, value: object) -> str:
    return f"{label}: {value}"


# ---------------------------------------------------------------------- one email, no Gmail
def explain_email(controller: Controller, sender: str, subject: str, body: str) -> str:
    """Show every decision the program would make about an email, without touching Gmail and
    without saving anything. The model is not asked here (use the model box for that)."""
    config = controller.config
    out: list[str] = [f"Email from {sender or '(no sender)'}, subject: {subject or '(none)'}"]
    dept = config.department_for_sender(sender.strip().lower())
    if dept is not None:
        out.append(f"1. The sender is in the department '{dept.name}'.")
        uses_model = config.smart.enabled and dept.use_local_llm
        out.append(f"   Uses the local model: {'yes' if uses_model else 'no'}")
        roots = config.roots_for(dept)
    else:
        out.append("1. The sender is in no department.")
        if config.smart.read_unlisted:
            out.append("   Reading mail from such senders is ON, so the steps below apply.")
        else:
            out.append(
                "   Reading mail from such senders is OFF (Settings): it would be left alone."
            )
        roots = None
    today = date.today()
    found, notes = exact_items(subject, body, controller.index, roots, found_only=True)
    out.append(f"2. Lines that are exactly folder names: {len(found)} found")
    for item, triage in found:
        out.append(f"   - {item.program}: {triage.state} ({triage.reason})")
    out.extend(f"   note: {n}" for n in notes)
    parsed = parse_request(subject, body, config.parsing, today=today)
    out.append(f"3. Ordinary reading of the text: {len(parsed.items)} name(s) read")
    any_found = bool(found)
    for item in parsed.items:
        triage = triage_item(item, controller.index, config, today=today, roots=roots)
        top = triage.candidates[:3]
        hit = any(c.score >= FOUND_SCORE for c in top)
        any_found = any_found or hit
        out.append(
            f"   - read as '{item.program}' ({triage.state}): "
            + ("; ".join(f"{c.item.name} {c.score:.0f}%" for c in top) or "no candidate")
        )
    out.append(
        f"4. Counts as found in the index (a candidate of {FOUND_SCORE:.0f}% or more): "
        f"{'YES' if any_found else 'NO'}"
    )
    if any_found and found:
        out.append(
            "   -> lines are exactly folder names: a request is created without asking the model."
        )
    elif any_found and config.smart.enabled:
        out.append(
            "   -> the local model is asked first whether it is a request; if not, it is skipped."
        )
    elif any_found:
        out.append("   -> a request is created for review without asking the model.")
    elif config.smart.enabled and config.smart.read_unlisted and dept is None:
        out.append("   -> the local model would be asked next (use 'Try the model' below).")
    elif dept is None:
        out.append("   -> nothing found and Smart reading is off: the mail is left alone.")
    else:
        out.append("   -> no folder found: the request waits for a person to pick one.")
    return "\n".join(out)


def try_model(controller: Controller, subject: str, body: str) -> str:
    """Ask the local model about a text and show its answer, prompt size and time."""
    clean_subject, clean_body = prepare(subject, body)
    reading = controller.smart_test(subject, body)
    prompt_chars = sum(len(m["content"]) for m in build_messages(clean_subject, clean_body, []))
    out = [
        reading_summary(reading),
        f"Why: {reading.reason or '(no reason given)'}",
        f"Prompt size: {prompt_chars} characters (email part {len(clean_body)})",
    ]
    if reading.answer:
        out.append(f"Answer as sent by the model: {reading.answer}")
    if reading.dropped:
        out.append("Dropped (not in the email): " + "; ".join(reading.dropped))
    return "\n".join(out)


# ---------------------------------------------------------------------- Gmail
def run_gmail_query(controller: Controller, query: str, limit: int = 15) -> str:
    """Raw results of a Gmail search: who, what, which labels."""
    gateway = controller.dev_gateway()
    ids = gateway.list_message_ids(query, limit)
    out = [f"Query: {query}", f"{len(ids)} message(s) returned (at most {limit} shown)"]
    for message_id in ids:
        msg = parse_message(gateway.get_message(message_id))
        seen = "already looked at" if controller.repo.is_processed(message_id) else "NEW"
        out.append(
            f"- {message_id}  {msg.received_at[:16]}  {msg.sender_email}  |  {msg.subject[:70]}"
            f"  |  labels: {', '.join(msg.label_ids)}  |  {seen}"
        )
    return "\n".join(out)


def account_info(controller: Controller) -> str:
    """The signed-in Gmail address, what it may do, and when its access expires."""
    out: list[str] = []
    try:
        out.append(_line("Gmail address", controller.dev_gateway().profile_email()))
    except Exception as exc:
        out.append(f"Gmail address: not available ({type(exc).__name__})")
    out.append(
        _line(
            "Scopes the app asks for",
            ", ".join(_short(s) for s in required_scopes(controller.config.gmail)),
        )
    )
    auth: Any = controller.auth
    if auth is not None and not controller.config.dev.fake_gmail:
        try:
            creds = auth.credentials(required_scopes(controller.config.gmail))
            out.append(_line("Access token expires", getattr(creds, "expiry", None) or "unknown"))
            granted = getattr(creds, "scopes", None)
            if granted:
                out.append(_line("Scopes granted", ", ".join(_short(s) for s in granted)))
        except Exception as exc:
            out.append(f"Sign-in: {type(exc).__name__}")
    if controller.config.dev.fake_gmail:
        out.append("Fake Gmail is ON: the mailbox is a set of test emails, nothing real is read.")
    return "\n".join(out)


def _short(scope: str) -> str:
    return scope.rsplit("/", 1)[-1]


def gmail_calls_text() -> str:
    lines = TRACE.call_lines()
    return "\n".join(lines) if lines else "No Gmail call yet since developer mode was switched on."


# ---------------------------------------------------------------------- index
def index_summary(controller: Controller) -> str:
    stats = controller.index.stats()
    out = [
        _line("Items in the index", stats.get("items", 0)),
        _line("Folders", stats.get("folders", 0)),
    ]
    for root in controller.config.all_roots():
        scan = controller.index.last_scan(root)
        if scan is None:
            out.append(f"{root}: never scanned completely")
        else:
            out.append(
                f"{root}: scanned {scan.get('finished_at')}, {scan.get('items', '?')} item(s)"
            )
    if not controller.config.all_roots():
        out.append("No storage folder is set up.")
    return "\n".join(out)


def index_search(controller: Controller, text: str) -> str:
    """How a name scores against the index, as a department-less search (all storage)."""
    config = controller.config
    triage = triage_item(
        RequestItem(program=text.strip(), raw=text.strip()),
        controller.index,
        config,
        today=date.today(),
    )
    out = [f"Search: {text.strip()!r}  ->  {triage.state}: {triage.reason}"]
    for cand in triage.candidates[:10]:
        flags = f"  flags: {', '.join(sorted(cand.flags))}" if cand.flags else ""
        out.append(f"{cand.score:5.1f}%  {cand.item.full_path}{flags}")
        out.extend(f"         {n}" for n in cand.notes[:3])
    if not triage.candidates:
        out.append("No candidate.")
    return "\n".join(out)


# ---------------------------------------------------------------------- the model
def model_status(controller: Controller) -> str:
    smart = controller.config.smart
    out = [
        _line("Smart reading", "on" if smart.enabled else "off"),
        _line("Reads mail of senders in no department", "yes" if smart.read_unlisted else "no"),
        _line("Model", smart.model),
        _line("Graphics card", "yes" if smart.use_graphics_card else "no"),
    ]
    try:
        status = controller.smart_status()
        out.append(_line("Program installed", "yes" if status.program_ready else "no"))
        out.append(_line("Model file installed", "yes" if status.model_ready else "no"))
        out.append(_line("Model", status.model_label))
        if status.missing_bytes:
            out.append(_line("Still to download (bytes)", status.missing_bytes))
    except Exception as exc:
        out.append(f"Status not available ({type(exc).__name__})")
    out.append(_line("Running now", "yes" if controller.smart_running() else "no"))
    return "\n".join(out)


# ---------------------------------------------------------------------- database (read only)
def table_rows(
    controller: Controller, table: str, limit: int = 100
) -> tuple[list[str], list[list[str]]]:
    """Column names and the newest rows of one allowed table. Secret-looking columns are hidden."""
    if table not in TABLES:
        raise ValueError(f"{table} may not be shown")
    with controller.repo.db.read() as conn:
        query = f"SELECT * FROM {table} ORDER BY 1 DESC LIMIT ?"  # noqa: S608  # nosec B608
        cur = conn.execute(query, (int(limit),))
        columns = [c[0] for c in cur.description]
        keep = [i for i, c in enumerate(columns) if not any(w in c.lower() for w in _SECRET_WORDS)]
        rows = [[_cell(row[i]) for i in keep] for row in cur.fetchall()]
    return [columns[i] for i in keep], rows


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= 300 else text[:300] + "..."


# ---------------------------------------------------------------------- everything, to copy
def diagnostics(controller: Controller, *, include_mail_text: bool = False) -> str:
    """One text to paste into a message: versions, settings with no secrets, the last check
    and the last log lines."""
    config = controller.config
    settings = config.model_dump()
    gmail = settings.get("gmail", {})
    gmail["client_secret_path"] = "(set)" if gmail.get("client_secret_path") else None
    settings.pop("default_reply_template", None)
    settings.pop("telegram", None)
    out = [
        f"ContentShare AI {__version__}",
        f"Python {sys.version.split()[0]} on {platform.platform()}",
        "",
        "== Settings (no secrets) ==",
        f"departments: {len(config.departments)}",
        f"storage folders: {len(config.all_roots())}",
        f"gmail: {gmail}",
        f"smart: {settings.get('smart')}",
        f"dev: {settings.get('dev')}",
        f"dry_run: {config.dry_run}",
        "",
        "== Account ==",
        account_info(controller),
        "",
        "== Index ==",
        index_summary(controller),
        "",
        "== Model ==",
        model_status(controller),
        "",
        "== Last mail check ==",
    ]
    last = TRACE.last_check()
    text = last.text() if last else "No mail check since developer mode was switched on."
    if not include_mail_text:
        text = _hide_subjects(text)
    out.append(text)
    out += ["", "== Gmail calls ==", gmail_calls_text(), "", "== Last log lines =="]
    out.extend(BUFFER.lines(logging.INFO)[-200:])
    return "\n".join(out)


def _hide_subjects(text: str) -> str:
    """Subjects and senders are mail text too: leave them out unless asked."""
    lines = []
    for line in text.splitlines():
        lines.append("* (subject and sender hidden)" if line.startswith("* ") else line)
    return "\n".join(lines)

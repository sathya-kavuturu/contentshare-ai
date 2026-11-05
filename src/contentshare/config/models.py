"""Validated application configuration."""

from __future__ import annotations

import ntpath
import os
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from contentshare.config.templates import (
    DEFAULT_REPLY_TEMPLATE,
    REPLY_PLACEHOLDERS,
    SUBFOLDER_PLACEHOLDERS,
    current_reply_template,
    validate_template,
)
from contentshare.security.paths import PathSafetyError, ensure_absolute_windows_path

SCHEMA_VERSION = 1

_EMAIL = re.compile(r"^[^@\s<>,;]+@[a-z0-9.-]+\.[a-z]{2,}$")
_DOMAIN = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}$")
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# Anyone in the world can get an address at these, so allowing the whole domain would let any
# stranger send requests. (Verification would pass for them too: it is their own account.)
PUBLIC_MAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com",
    "yahoo.com", "ymail.com", "icloud.com", "me.com", "aol.com", "proton.me",
    "protonmail.com", "gmx.com", "mail.com", "zoho.com", "yandex.com", "rediffmail.com",
})  # fmt: skip


def _key(path: str) -> str:
    """A path in a form where equal folders compare equal (case, slashes, trailing slash)."""
    return ntpath.normcase(ntpath.normpath(path)).rstrip("\\")


def paths_overlap(a: str, b: str) -> bool:
    """True if the two folders are the same or one lies inside the other."""
    ka, kb = _key(a), _key(b)
    return ka == kb or ka.startswith(kb + "\\") or kb.startswith(ka + "\\")


def inside_system_folder(path: str) -> str | None:
    """The Windows or Program Files folder ``path`` is in, if any."""
    for variable in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        folder = os.environ.get(variable)
        if (
            folder
            and paths_overlap(path, folder)
            and not _key(folder).startswith(_key(path) + "\\")
        ):
            return folder
    return None


def _check_root(value: str) -> str:
    try:
        ensure_absolute_windows_path(value)
    except PathSafetyError as exc:
        raise ValueError(str(exc)) from exc
    return value.rstrip("\\/") if len(value.rstrip("\\/")) > 2 else value


class Department(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=80)
    sender_emails: list[str] = Field(default_factory=list)
    sender_domains: list[str] = Field(default_factory=list)
    destination_root: str
    subfolder_pattern: str = "{program}"
    reply_template: str | None = None
    # True: the emails give folder names exactly as they are on the server, so only folders with
    # exactly those names are used and nothing is guessed. False: the closest matches are
    # suggested and the folder can also be browsed for.
    exact_folder_names: bool = False
    # True: content can also be picked by hand from anywhere on this PC or network (File
    # Explorer style), not only inside the storage folders. Off by default.
    browse_anywhere: bool = False
    # This department's own folders to search (a server, a drive, a folder). Empty: it uses the
    # storage folders from Settings → General, as every department did before.
    source_roots: list[str] = Field(default_factory=list)
    # True: when nothing is found in the folders above, the PC's own drives can be searched too
    # (never network drives, and never Windows or Program Files). Off by default.
    search_whole_pc: bool = False
    # True: when the emails of this department do not contain exact folder names, the local
    # language model reads them too (only while Settings → Smart reading is on).
    use_local_llm: bool = False

    @property
    def may_pick_anywhere(self) -> bool:
        """Content outside the department's folders may be used: picked by hand, or found by the
        whole-PC search."""
        return self.browse_anywhere or self.search_whole_pc

    @field_validator("source_roots")
    @classmethod
    def _own_roots(cls, values: list[str]) -> list[str]:
        out: list[str] = []
        for value in values:
            checked = _check_root(value.strip())
            if all(_key(checked) != _key(seen) for seen in out):
                out.append(checked)
        return out

    @field_validator("sender_emails")
    @classmethod
    def _emails(cls, values: list[str]) -> list[str]:
        out = [v.strip().lower() for v in values]
        for v in out:
            if not _EMAIL.match(v):
                raise ValueError(f"Not a valid email address: {v!r}")
        return out

    @field_validator("sender_domains")
    @classmethod
    def _domains(cls, values: list[str]) -> list[str]:
        out = [v.strip().lower().lstrip("@") for v in values]
        for v in out:
            if not _DOMAIN.match(v):
                raise ValueError(f"Not a valid domain: {v!r}")
            if v in PUBLIC_MAIL_DOMAINS:
                raise ValueError(
                    f"@{v} is a public mail service: anyone can have an address there. "
                    "List the specific sender addresses instead."
                )
        return out

    @field_validator("destination_root")
    @classmethod
    def _dest(cls, value: str) -> str:
        value = _check_root(value)
        protected = inside_system_folder(value)
        if protected:
            raise ValueError(f"Copies must not go into a Windows system folder ({protected})")
        return value

    @field_validator("subfolder_pattern")
    @classmethod
    def _pattern(cls, value: str) -> str:
        validate_template(value, SUBFOLDER_PLACEHOLDERS)
        if re.match(r"^([A-Za-z]:|[\\/])", value) or ".." in re.split(r"[\\/]", value):
            raise ValueError("Subfolder pattern must be relative and must not contain '..'")
        if not value.strip():
            raise ValueError("Subfolder pattern must not be empty")
        return value

    @field_validator("reply_template")
    @classmethod
    def _reply(cls, value: str | None) -> str | None:
        if value is not None:
            validate_template(value, REPLY_PLACEHOLDERS)
            return current_reply_template(value)
        return value

    @model_validator(mode="after")
    def _has_sender(self) -> Department:
        if not self.sender_emails and not self.sender_domains:
            raise ValueError("A department needs at least one sender email or domain")
        return self

    def matches_sender(self, email: str) -> bool:
        email = email.strip().lower()
        return email in self.sender_emails or email.rpartition("@")[2] in self.sender_domains


class CopySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hash_algorithm: Literal["xxh3_128", "blake3", "sha256"] = "xxh3_128"
    # Every file is always checked against the original while copying. This only decides whether
    # a checksum file is also left in the copied folder for checking again later.
    write_checksum_file: bool = False
    chunk_mb: Annotated[int, Field(ge=1, le=64)] = 8
    concurrency: Annotated[int, Field(ge=1, le=4)] = 2
    # Files of one request copied at the same time. Each is still checked on its own.
    files_at_once: Annotated[int, Field(ge=1, le=8)] = 4
    max_attempts: Annotated[int, Field(ge=1, le=10)] = 3
    free_space_margin_pct: Annotated[int, Field(ge=0, le=50)] = 5


# How often mail can be checked by itself: 0 means once a day at a set time.
CHECK_INTERVALS = (0, 15, 30, 60, 120, 180, 360, 720)


class UpdateSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Ask GitHub (the project's releases page) whether a newer version exists when the program
    # starts. Nothing about this PC or its settings is sent; it is a plain read of a public page.
    check_on_start: bool = True


# A Telegram chat: a number (groups are negative) or @name of a public channel.
TELEGRAM_CHAT = re.compile(r"^(-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{4,31})$")


class TelegramSettings(BaseModel):
    """Alerts to a Telegram group when new requests wait for approval. Only counts are sent.
    The bot token is not here: it is kept in Windows Credential Manager."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    chat_id: str = ""

    @field_validator("chat_id")
    @classmethod
    def _chat(cls, value: str) -> str:
        value = value.strip()
        if value and not TELEGRAM_CHAT.match(value):
            raise ValueError("The Telegram chat number is not valid (a group looks like -100123…)")
        return value


class ScheduleSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    daily_check_time: str = "09:00"
    # 0 = once a day at ``daily_check_time``; otherwise a check every this many minutes.
    check_every_minutes: int = 0
    # With a check every so many minutes: only between these times (local time, 24 hours). From
    # later than to means overnight (for example 22:00 to 06:00). 00:00 to 23:59 is all day.
    window_from: str = "00:00"
    window_to: str = "23:59"
    run_missed_check_on_startup: bool = True
    start_with_windows: bool = False
    daily_check_enabled: bool = True
    close_to_tray: bool = True  # the window's close button hides it; Quit is in the tray menu
    notifications: bool = True  # pop-ups (counts only, never email text)

    @field_validator("daily_check_time", "window_from", "window_to")
    @classmethod
    def _time(cls, value: str) -> str:
        if not _TIME.match(value):
            raise ValueError("Time must be HH:MM in 24-hour format")
        return value

    @field_validator("check_every_minutes")
    @classmethod
    def _interval(cls, value: int) -> int:
        if value not in CHECK_INTERVALS:
            raise ValueError(f"Check interval must be one of {sorted(CHECK_INTERVALS)} minutes")
        return value


class MatchSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_ready_score: Annotated[int, Field(ge=50, le=100)] = 85
    ambiguity_gap: Annotated[int, Field(ge=0, le=50)] = 5
    llm_extraction_enabled: bool = False


DEFAULT_LANGUAGES: dict[str, list[str]] = {
    "english": ["en", "eng"],
    "hindi": ["hi", "hin"],
    "tamil": ["ta", "tam"],
    "telugu": ["te", "tel"],
    "kannada": ["kn", "kan"],
    "malayalam": ["ml", "mal"],
    "bengali": ["bn", "ben"],
    "marathi": ["mr", "mar"],
    "gujarati": ["gu", "guj"],
    "odia": ["or", "odi"],
    "punjabi": ["pa", "pun"],
}
DEFAULT_EXCLUDES = [
    "$RECYCLE.BIN", "System Volume Information", "Thumbs.db", "desktop.ini", ".DS_Store", "~$*",
]  # fmt: skip


class ParsingSettings(BaseModel):
    """How request emails are read. Everything here is plain data the user can edit."""

    model_config = ConfigDict(extra="forbid")

    day_first: bool = True  # "03/04/2026" means 3 April, not March 4
    languages: dict[str, list[str]] = Field(default_factory=lambda: dict(DEFAULT_LANGUAGES))
    program_patterns: list[str] = Field(
        default_factory=list
    )  # regexes with a (?P<program>...) group
    stop_phrases: list[str] = Field(default_factory=list)  # extra words to ignore in requests

    @field_validator("program_patterns")
    @classmethod
    def _patterns(cls, values: list[str]) -> list[str]:
        for pattern in values:
            try:
                compiled = re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"Invalid pattern {pattern!r}: {exc}") from exc
            if "program" not in compiled.groupindex:
                raise ValueError(f"Pattern {pattern!r} needs a (?P<program>...) group")
        return values

    @field_validator("languages")
    @classmethod
    def _languages(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        return {
            k.strip().lower(): [a.strip().lower() for a in v if a.strip()] for k, v in value.items()
        }


class IndexSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exclude_names: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    index_files: bool = True  # False = folders only, for very large servers


class GmailSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_secret_path: str | None = None  # the OAuth client file you download from Google Cloud
    lookback_days: Annotated[int, Field(ge=1, le=365)] = 30
    max_messages: Annotated[int, Field(ge=1, le=500)] = 100  # per check
    # Applying a label needs the broader gmail.modify permission. Turn this off to stay on
    # read + send only; the app's own database still prevents processing an email twice.
    apply_label: bool = True
    processed_label: str = "ContentShareAI/Processed"
    # Accept a request as verified only when the mail provider reports the sender passed
    # DMARC (or SPF and DKIM). Only results added by these servers are believed.
    require_authentication: bool = True
    trusted_authserv_ids: list[str] = Field(default_factory=lambda: ["mx.google.com"])

    @field_validator("processed_label")
    @classmethod
    def _label(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 100 or any(ord(c) < 32 for c in value):
            raise ValueError("Label must be 1-100 printable characters")
        return value

    @field_validator("trusted_authserv_ids")
    @classmethod
    def _servers(cls, values: list[str]) -> list[str]:
        return [v.strip().lower() for v in values if v.strip()]


class SmartSettings(BaseModel):
    """Reading requests from senders that are in no department, with a small language model that
    runs on this PC (nothing is sent anywhere). Everything it finds is checked against the
    storage server and always waits for the person's approval."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False  # the master switch: off means the model is never started
    # With the switch on: also read mail from senders that are in no department. (Departments use
    # the model only if their own tick is set.)
    read_unlisted: bool = True
    model: Literal["recommended", "small"] = "recommended"
    use_graphics_card: bool = False  # the Vulkan build, for a graphics card with 2 GB or more
    learn: bool = True  # keep the person's corrections to show the model as examples
    max_per_check: Annotated[int, Field(ge=1, le=100)] = 20  # messages the model reads per check
    keep_loaded_minutes: Annotated[int, Field(ge=1, le=120)] = 10  # then it is stopped, freeing RAM


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    source_roots: list[str] = Field(default_factory=list)
    departments: list[Department] = Field(default_factory=list)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)
    updates: UpdateSettings = Field(default_factory=UpdateSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    copy_options: CopySettings = Field(default_factory=CopySettings)
    matching: MatchSettings = Field(default_factory=MatchSettings)
    parsing: ParsingSettings = Field(default_factory=ParsingSettings)
    index: IndexSettings = Field(default_factory=IndexSettings)
    gmail: GmailSettings = Field(default_factory=GmailSettings)
    smart: SmartSettings = Field(default_factory=SmartSettings)
    appearance: Literal["system", "light", "dark"] = "system"  # follow Windows, or force one
    # Window layout the person chose (remembered, never needed for anything to work)
    user_name: str = ""  # who is using the app (asked when it opens); shown in the History
    # Set once the first-start questions were answered or put away (or departments existed), so
    # deleting every department later does not bring them back.
    setup_done: bool = False
    sidebar_collapsed: bool = False  # the menu on the left folded to icons
    compact_requests: bool = False  # request cards folded to one short line
    auto_reply_default: bool = False
    # Off: every request waits for a person to approve it. On: a request that is Ready and matched
    # at 100% from a verified sender is copied, verified and answered without anyone approving.
    auto_copy_certain: bool = False
    # Rehearsal: approving checks everything (paths, space, names) and reports what would be
    # copied, but nothing is copied and no email is sent.
    dry_run: bool = False
    default_reply_template: str = DEFAULT_REPLY_TEMPLATE

    @field_validator("source_roots")
    @classmethod
    def _roots(cls, values: list[str]) -> list[str]:
        return [_check_root(v) for v in values]

    @field_validator("default_reply_template")
    @classmethod
    def _tmpl(cls, value: str) -> str:
        validate_template(value, REPLY_PLACEHOLDERS)
        return current_reply_template(value)

    @model_validator(mode="after")
    def _unique(self) -> AppConfig:
        if self.departments and self.all_roots():
            self.setup_done = True
        names = [d.name.lower() for d in self.departments]
        if len(names) != len(set(names)):
            raise ValueError("Department names must be unique")
        # One sender must never belong to two departments, or content could go to the wrong one.
        seen: dict[str, str] = {}
        for dept in self.departments:
            for key in [*dept.sender_emails, *(f"@{d}" for d in dept.sender_domains)]:
                if key in seen and seen[key] != dept.name:
                    raise ValueError(f"{key} is assigned to both '{seen[key]}' and '{dept.name}'")
                seen[key] = dept.name
        self._check_folders()
        emails = {k: v for k, v in seen.items() if not k.startswith("@")}
        for email, owner in emails.items():
            domain_owner = seen.get("@" + email.rpartition("@")[2])
            if domain_owner and domain_owner != owner:
                raise ValueError(
                    f"{email} ({owner}) falls under the domain rule of "
                    f"'{domain_owner}'; make them consistent"
                )
        return self

    def roots_for(self, dept: Department | None) -> list[str]:
        """The folders this department's requests are searched in: its own, or else the storage
        folders from Settings → General."""
        if dept is not None and dept.source_roots:
            return list(dept.source_roots)
        return list(self.source_roots)

    def all_roots(self) -> list[str]:
        """Every folder that is indexed: the general storage folders and each department's own."""
        out: list[str] = []
        for root in [*self.source_roots, *(r for d in self.departments for r in d.source_roots)]:
            if all(_key(root) != _key(seen) for seen in out):
                out.append(root)
        return out

    def _check_folders(self) -> None:
        """Where copies go must never be (or contain) the archive being read, or another
        department's folder: content would land where the wrong people can see it."""
        for dept in self.departments:
            for root in self.all_roots():
                if paths_overlap(dept.destination_root, root):
                    raise ValueError(
                        f"The folder for '{dept.name}' ({dept.destination_root}) overlaps the "
                        f"storage folder {root}. Copies must go outside the archive."
                    )
        for i, first in enumerate(self.departments):
            for second in self.departments[i + 1 :]:
                if paths_overlap(first.destination_root, second.destination_root):
                    raise ValueError(
                        f"The folders of '{first.name}' and '{second.name}' are the same or "
                        "inside each other. Each department needs its own folder."
                    )

    def department_for_sender(self, email: str) -> Department | None:
        """Exact email matches win over domain matches."""
        email = email.strip().lower()
        for dept in self.departments:
            if email in dept.sender_emails:
                return dept
        for dept in self.departments:
            if dept.matches_sender(email):
                return dept
        return None

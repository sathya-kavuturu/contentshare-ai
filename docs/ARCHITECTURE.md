# ContentShare — Architecture & Wireframes

## 1. Principles

1. **Nothing happens without a click.** Every copy and every reply is gated by an explicit confirmation.
2. **A copy is only "done" when the destination has been read back and its hash matches the source.**
3. **Email is untrusted input.** Plain text only; the sender is verified; email text never becomes a file path.
4. **Core logic has no UI and no Gmail imports**, so it can be tested without either.
5. **Crash-safe by journal.** Every state change is written to SQLite before the action it describes.

## 2. Layers

```
src/contentshare/
  config/      pydantic models, YAML load/save, reply-template renderer
  security/    path safety (sanitise, contain, reserved names, ADS, junctions)
  storage/     SQLite connection, migrations, repository, audit log
  jobs/        request state machine, copy queue (phase 2)
  copy_engine/ verified copy: temp file -> hash -> fsync -> read-back -> atomic rename (phase 2)
  index/       FTS5 index of the storage server (phase 3)
  parsing/     email text -> request lines, no I/O (phase 3)
  matching/    rapidfuzz scoring of lines against the index (phase 3)
  gmail/       OAuth, fetch, label, threaded reply, fake service for tests (phase 4)
  app/         controller: everything a screen does, no Qt (phase 5); scheduler, tray, toasts (phase 6)
  ui/          PySide6 screens; they only call the controller and show what it returns (phase 5)
```

Dependency rule: arrows point downward only. `copy_engine`, `parsing`, `matching`, `security`
and `config` import nothing from `ui`, `gmail` or `app`.

```
 ui / app  ──►  jobs  ──►  copy_engine ──► security
    │            │  ╲
    ▼            ▼   ╲──► storage
  gmail ──► parsing ──► matching ──► index ──► storage
```

## 3. Data flow

```
 Gmail ──fetch──► sender allowlist ──► Authentication-Results check
                                              │ pass            │ fail
                                              ▼                 ▼
                                  parse lines ─► match     Needs review (Suspicious)
                                              │
                         READY / NEEDS_REVIEW (you pick the candidate)
                                              │ you click Approve
                                              ▼
              APPROVED ─► COPYING ─► VERIFIED ─► (auto or manual) reply in thread ─► REPLIED
                              │
                              └─► FAILED (reason + Retry; no "shared" reply is ever sent)
```

## 4. Request state machine

| From            | Allowed next                                   |
|-----------------|------------------------------------------------|
| NEW             | NEEDS_REVIEW, READY, SKIPPED, REJECTED         |
| NEEDS_REVIEW    | READY, SKIPPED, REJECTED                       |
| READY           | APPROVED, NEEDS_REVIEW, SKIPPED, REJECTED      |
| APPROVED        | COPYING, READY, SKIPPED                        |
| COPYING         | VERIFIED, FAILED, APPROVED (crash recovery)    |
| FAILED          | APPROVED (retry), SKIPPED                      |
| VERIFIED        | REPLIED                                        |
| REPLIED, SKIPPED, REJECTED | — terminal                          |

"Suspicious" sender is a flag (`auth_status`) on the email, not a state: a suspicious request can
only leave NEW through NEEDS_REVIEW, so you always look at it first.

## 5. Copy integrity (phase 2 design)

`pre-flight → copy to name.ext.cspartial (8–16 MB chunks, hashing the source as it is read)
→ flush + fsync → re-open destination uncached and hash it → compare → check source size/mtime
unchanged → atomic rename → write checksum manifest`. Retries with backoff; job resumes per file.

## 6. Storage

SQLite (WAL, foreign keys on, `busy_timeout`). Tables: `emails`, `requests` (one row per requested
item), `jobs`, `job_files`, `replies`, `audit_log` (append-only, enforced by triggers),
`content_index` (FTS5, phase 3). Config is a YAML file under `%APPDATA%\ContentShare`, written
atomically with a `.bak` of the previous version. OAuth tokens live only in Windows Credential Manager.

## 7. Wireframes

### Requests (default screen)

```
┌──────────────┬──────────────────────────────────────────────────────────────────────┐
│  ContentShare│  Requests                       [ Check now ]  last check 09:00 ✓     │
│              │  ───────────────────────────────────────────────────────────────────  │
│ ▌Requests (3)│  ☐ Select all        [ Approve selected ]  [ Copy selected ]          │
│  Active      │                                                                        │
│  copies  (1) │  ┌──────────────────────────────────────────────────────────────────┐ │
│  History     │  │ #12  ✔ Ready                              received today 08:41    │ │
│  Settings    │  │ From   Anita R. <anita@dept.org>  ·  Media Team                  │ │
│              │  │ Re     "Please share Program X recording of 12 Oct"   ▸ show text │ │
│              │  │ Found  \\storage01\Programs\2026\2026-10-12_ProgramX_EN           │ │
│              │  │        48.2 GB · 37 files · confidence ●●●●○ 92%   [ Change ]     │ │
│              │  │ Copy to  \\nas02\media\Incoming\ProgramX\2026-10-12   [ Edit ]    │ │
│              │  │ ☑ Confirm copy     ☑ Send reply automatically when verified       │ │
│              │  │ [ Approve ]  [ Skip ]  [ Reject… ]  [ Open email in Gmail ]       │ │
│              │  └──────────────────────────────────────────────────────────────────┘ │
│              │  ┌──────────────────────────────────────────────────────────────────┐ │
│              │  │ #13  ⚠ Needs review · Suspicious sender                           │ │
│              │  │ Top matches: 1) …  2) …  3) …  4) …  5) …      [ Search manually ]│ │
│              │  └──────────────────────────────────────────────────────────────────┘ │
└──────────────┴──────────────────────────────────────────────────────────────────────┘
```

### Active copies

```
│  #12 Program X → Media Team          ▶ Copying   file 14/37  ProgramX_part3.mxf       │
│  ████████████░░░░░░░░░░░░  51%  24.6 / 48.2 GB   212 MB/s   ETA 1 min 52 s            │
│  Stage: Copy ▸ Verify ▸ Finalise        [ Pause ]  [ Cancel ]                          │
│  When finished: ☑ send reply        (or)  [ Send reply… ] shows an editable preview    │
```

### Reply preview (after VERIFIED)

```
│  To: anita@dept.org     In reply to: "Please share Program X recording…"               │
│  ┌────────────────────────────────────────────────────────────────────────────────┐  │
│  │ Hello Anita,                                                                    │  │
│  │ Program X (12 Oct) — 37 files, 48.2 GB — has been shared at \\nas02\media\…     │  │
│  └────────────────────────────────────────────────────────────────────────────────┘  │
│  ✔ All 37 files verified (xxHash3)                     [ Send reply ]  [ Not now ]    │
```

Status is always shown as **icon + word + colour** (✔ Ready, ⚠ Needs review, ▶ Copying,
✔✔ Verified, ✖ Failed, ✉ Replied), never colour alone.

### As built (phase 5)

The screens follow the wireframes above, with these differences: *Approve* starts the copy at
once (the *Confirm copy* tick is the confirmation, and there is no separate "Copy selected"
step); bulk *Approve selected* asks once with the total size and leaves out unverified
senders; a dry-run banner is shown whenever dry-run mode is on. The UI uses plain PySide6 with a
Fluent-style theme because `PySide6-Fluent-Widgets` is GPLv3. Threads: copies run in the copy
queue, Gmail and folder scans in a small worker pool, and results return to the GUI thread
through `ui/bridge.py`; the screens never block on the network.

### As built (phase 6)

`app/scheduler.py` (when a check is due; wall-clock based, no threads), `app/startup.py`
(Run key / Task Scheduler), `app/notifications.py` (what a pop-up says; counts only) are Qt-free
and unit-tested with a fake clock and fake backends. `ui/automation.py` polls the scheduler
every 15 s and runs due checks on the worker pool; `ui/tray.py`, `ui/icons.py` and
`ui/single_instance.py` (file lock + user-only named pipe) are the only new Qt parts. The
application no longer quits when its last window closes: quitting is explicit, from the tray
menu or when the tray is unavailable.

## 8. Open items (waiting on you)

Sample emails, department table, source roots, example folder names, reply wording, daily check time.
Phase 1 does not depend on them; Phase 3 (matching) does.

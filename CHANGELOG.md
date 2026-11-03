# Changelog

All notable changes. Format follows [Keep a Changelog](https://keepachangelog.com/); the project
uses [semantic versioning](https://semver.org/).

## [Unreleased]

### Added
- **Update check:** when the program starts (switchable in Settings → General, or on demand with
  *Check for updates now*) it looks for a newer release on GitHub. A box at the bottom left of the
  window offers **Download**; when the installer has been downloaded and checked against its
  published SHA-256 the box shows **Update** and **Cancel**. Nothing is installed without the
  click, and the update waits while copies are running.
- **In-app Help:** a Help screen with *Getting started*, *Connect Gmail* (the whole Google Cloud
  walk-through), *Using the app*, *Settings explained* (every setting) and *Troubleshooting*. It is
  bundled in the program, works offline, and the self-test checks it is present.
- Settings → Gmail has a "How do I get this file?" button that opens the Gmail help.

### Fixed
- **Storage folders could not be edited or removed in the first-run setup**, and could not be
  edited in Settings. Both now use one list with **Add folder…**, a type-or-paste box, **Edit**
  (also double-click or F2) and **Remove** (also the Delete key). The same folder written two ways
  is kept once.

## [0.1.0] - 2026-11-03

First complete build. Developed and tested against invented data and a fake Gmail only; it has
**not** yet been used with a real mailbox, a real storage server or on a second PC.

### Added
- **Settings and database (phase 1):** validated YAML settings (atomic save with `.bak`), SQLite
  database with migrations, an append-only audit log, the request state machine
  (`NEW → NEEDS_REVIEW | READY → APPROVED → COPYING → VERIFIED → REPLIED`, plus
  `SKIPPED`, `REJECTED`, `FAILED`) and path-safety checks.
- **Verified copy engine (phase 2):** temporary-name copy with hash while reading, `fsync`, uncached
  read-back and compare, source-change detection, atomic no-overwrite rename, retries, resume after a
  crash, checksum file, pause/resume/cancel, benchmark script.
- **Finding the content (phase 3):** plain-text email parser (dates, language, part, format, lists),
  SQLite full-text index of the storage server, fuzzy matcher, Ready / Needs-review triage.
- **Gmail (phase 4):** OAuth sign-in with the token in Windows Credential Manager, allow-listed senders,
  sender verification from `Authentication-Results`, threaded replies to the original sender,
  crash-safe replies (never sent twice), disconnect with revoke.
- **Screens (phase 5):** Requests, Active copies, History (with CSV export), Settings, first-run
  setup, dry-run mode, light and dark themes.
- **Daily check, tray, notifications (phase 6):** scheduled check (also after sleep or a missed run),
  start with Windows, tray icon, count-only pop-ups, one copy of the app per user.
- **Packaging (phase 7):** PyInstaller build with a built-in `--selftest` that gates the build, Inno
  Setup installer (per user, no administrator rights), user guide, packaging guide, CI package job.
- **Security review (phase 8):** `SECURITY.md` with the threat model and findings.

### Security
- All on-screen text is plain: email-derived text can no longer be interpreted as HTML (which could
  load an image from a network path and leak the user's Windows sign-in hash).
- Public mail domains (`gmail.com`, …) cannot be allowed as a whole.
- Copy folders may not overlap the archive, each other, or Windows/Program Files folders.
- Logs never include exception messages (only type and location), so quoted email text cannot leak.
- The parser limits subject length itself; the copy engine's only delete refuses non-temporary names.
- `.gitignore` covers signing certificates.

### Known limits
- Not code-signed; Windows SmartScreen will warn.
- Optional AI-assisted email reading (in the specification, off by default) is not built.
- Replies are sent one per request line; a combined reply per email is not implemented.
- Benchmarks are for a local disk; the network share has not been measured.

# Changelog

All notable changes. Format follows [Keep a Changelog](https://keepachangelog.com/); the project
uses [semantic versioning](https://semver.org/).

## [Unreleased]

### Added
- **Telegram alerts.** Optionally, ContentShare posts a short message to a Telegram group when a
  check finds new requests ("3 new requests waiting for approval (2 ready, 1 need a look)"). Set
  it up in Settings → General → Telegram: paste the bot token, press **Find my group**, then
  **Send a test message**. Only counts are sent, never email text, and the bot token is kept in
  Windows Credential Manager, not in the settings file. It is off until you switch it on.

### Changed
- **Cancelling a copy** names the program in its question, and "No" is now the default answer, so
  pressing Enter cannot stop a copy by accident.
- **One email is one request on screen.** An email that lists several folders is now one card with a
  section for each folder (status, the path chosen, its own buttons), instead of one card per folder.
  **Approve and copy**, **Skip all** and **Reject…** work on the whole email, the number on the
  Requests icon counts emails, and the copy, check and History records still go folder by folder.
- **One reply per email.** The reply names every folder that was copied and checked. With the
  automatic reply it is sent when the last folder being copied is done, and only once.
- **All the paths are listed.** When a folder name exists in several places on the storage server,
  every path is shown (a long list scrolls inside the card) and you choose which one to use; the
  list used to stop at five.

### Fixed
- **A folder name that ends in a date was not found** (for example the line `Nov 22-28  Amber Step 4
  English 31-oct-2026`). The free-text reading took the dates and the language out and looked for
  something else. Now every line of the email is first looked up as the exact name of a folder (or
  file) on the storage server, with or without a date range written before it, for every
  department, whether or not *The emails give exact folder names* is ticked. Only when no line is
  a folder name does the closest-match search run, as before.
- **A folder made after the last scan was "not found".** The storage index is read about once a
  day, so a folder created since then (for example this week's programs) was missing. When a
  request is not found and the storage was last read more than 10 minutes ago, ContentShare now
  reads the storage folders again once and looks again before marking it *Needs review*.
- **A single file was copied into a folder named after the folder it was kept in.** A file is now
  copied straight into the department's folder, without that extra folder.
- **A window no longer flashes when Settings is opened.** Looking up the Windows start-up task
  started a console program that opened and closed a window; it now runs hidden.
- **Help no longer shows a topic twice** after an update. Old help pages left behind by an earlier
  version were listed beside the renamed ones; the installer now clears the old program files
  before an update, and Help shows each topic once in any case.

### Added
- **History shows how long each copy took.** A new *Copy time* column (and, when a request is
  selected, its average speed) shows the time from the first file to the last check, and the
  exported CSV has `copy_seconds`. Copy start and end times are now stored to the millisecond, so
  short copies no longer read as 0 s. Requests copied by earlier versions show the time to the
  nearest second.
- **Fold the menu on the left** to icons only (**« Fold menu** at its bottom; names appear as tooltips,
  and the Requests icon keeps its count). It opens by itself if an update is found.
- **Fold requests to one short line** showing the title, sender name and department: an arrow on each
  card, and one **Collapse all / Expand all** button above the list. Ticks are kept, new requests follow
  the last choice, and both choices are remembered.
- **Each department can have its own folders to search** (Settings → Departments → *Search in*): a
  server, drive or folder per department. A department with none uses the general storage folders,
  as before. Requests are looked for only in the requesting department's folders, and a pick must be
  inside them. All of these folders are indexed.
- **Search the whole PC when nothing is found** (per department, off by default). *Choose content*
  searches this PC's own drives by name, starting by itself when nothing was found there, with a
  Stop button. It only suggests: nothing found this way is marked Ready, and it skips network drives,
  Windows and Program Files, recycle bins, links and the departments' copy folders.
- **Check for requests more often than once a day.** Settings → General → *How often*: once a day
  at a set time, or every 15 minutes, 30 minutes, hour, 2, 3, 6 or 12 hours. A check still only
  reads mail and adds cards; nothing is copied or sent without a click. Old settings keep working
  as once a day.
- The theme choice is remembered as soon as it is chosen (no need to press Save).
- **Help: "How searching works"** explains in plain words how the mailbox is searched (which mail is
  read, what is checked, how the subject and body are understood) and how folders and files are
  found (the index, the closest-match scoring, exact folder names, choosing it yourself), with a
  table of what each "Needs review" reason means.
- **Allow choosing from anywhere, per department** (Settings → Departments, and the first-run
  setup; off by default). When on, *Browse for a folder… / a file…* accepts content anywhere on the
  PC or network, for departments whose content is not kept in one place. It is refused inside
  Windows or Program Files, for a whole drive, and when it overlaps the department's destination,
  and is checked again at approval and before the copy. Everything copied is still verified.
- **Exact folder names, per department.** In Settings → Departments (and in the first-run setup)
  tick *The emails give exact folder names* for a department whose requests name the folder as it
  is on the storage server. Each name in the email (one per line, in a list, or in quotes) is looked
  up as a folder with exactly that name; one folder is *Ready*, several are *Needs review*, and a
  name that is not found is reported. Nothing is guessed. Leave it off for departments whose emails
  are free-form: you get the closest matches and can browse for the folder yourself.
- **Update check:** when the program starts (switchable in Settings → General, or on demand with
  *Check for updates now*) it looks for a newer release on GitHub. A box at the bottom left of the
  window offers **Download**; when the installer has been downloaded and checked against its
  published SHA-256 the box shows **Update** and **Cancel**. Nothing is installed without the
  click, and the update waits while copies are running.
- **In-app Help:** a Help screen with *Getting started*, *Connect Gmail* (the whole Google Cloud
  walk-through), *Using the app*, *Settings explained* (every setting) and *Troubleshooting*. It is
  bundled in the program, works offline, and the self-test checks it is present.
- Settings → Gmail has a "How do I get this file?" button that opens the Gmail help.

- **Browse for the content yourself** when choosing it for a request: *Browse for a folder…* and
  *Browse for a file…* in the "Choose content" window, opening at the storage server. The pick must
  be inside a storage folder in Settings; it need not be in the index.
- **Appearance** in Settings → General: *Same as Windows*, *Light* or *Dark*, applied at once.

### Changed
- **Faster copies, with the same checking.** The next piece of a file is now read (and its checksum
  worked out) while the current piece is being written, and the read-back that verifies the copy
  reads ahead the same way. On the development PC a large file copies about 40% faster. Several
  files of one request are also copied at the same time (Settings → Matching and copying →
  *Files at the same time*, 1–8, default 4; 1 gives the old one-by-one behaviour), which is
  what helps most with many small files. Every file still goes through copy, flush to disk,
  read-back from the disk, comparison and rename on its own, and a file that fails is never put under
  its real name.
- **A new look**, built from the Linear design analysis in the `awesome-design-md` collection, in
  both light and dark: a near-black canvas with charcoal panels and hairline borders (soft grey and
  white in light), one lavender accent for the main action, the selection and the focus ring, and
  a quieter sidebar with icons and a count pill. Request cards are denser and easier to scan (title,
  sender line, status chip, then the matches), long paths are shortened in the middle instead of
  stretching the window, tables have roomy rows and coloured status words, Settings is grouped
  into sections, and the Help text, the first-run setup and the update box follow the same style.
  Every colour pair is checked for contrast by a test.
- **No checksum file is left in the copied folder any more.** Every file is still checked against
  the original while it is copied; the extra `<folder>.checksums.xxhash` file is now optional
  (Settings → Matching and copying → *Keep a checksum file in each copied folder*, off by default).
- Times in the lists, the request cards, the audit trail and Settings → Index are now shown on the
  computer's own clock. They were shown in UTC, which is a different time in most time zones.
- The first screen of the first-run setup no longer has a Back button.
- The folder pickers always open in a local folder. Windows used to reopen them in the last folder
  used, which after browsing a network share meant waiting for that server.
- Settings and Help are laid out in the background shortly after start, so they open at once.

### Fixed
- **The security scan (bandit) failed in CI** on three database queries whose only built-in part is
  the list of `?` marks for the folders searched. They are marked as reviewed, as the others are,
  so the release is published again. The tests themselves had passed.
- **Exact folder names missed lines that began with a date.** A line such as `Nov 22-28 Amber Step 4
  Hindi v002` is now looked up as the folder `Amber Step 4 Hindi v002` when no folder has the whole
  line as its name. A date after the name is kept, as it can be part of it.
- **Exact names only looked for folders.** When no folder has the name, a file with exactly that
  name (with or without its extension) is used, so `Marathi Orchid 6th of November` finds the
  video file. A folder is still preferred.
- A card removed from the Requests list could linger on screen for a moment, showing through the new
  cards; it is now hidden at once.
- **Settings flipped back to light mode and was slow to open.** Opening Settings re-applied the saved
  theme, which undid an unsaved Dark choice and restyled the whole window, and it read the Gmail
  sign-in, Windows start-up entry and index size before showing anything. The boxes are now filled
  again only when the settings changed elsewhere (what you typed is kept), those readings are made
  in the background, and a Settings screen that is slow to open is noted in the log file.
- **The Theme drop-down (and the other drop-downs) looked cramped**: the choices had no room around
  them. They now have padding and a rounded highlight, and the list is as wide as its longest
  choice. The time and number boxes no longer put their text against the border.
- **The update download looked stuck at 0%.** The checksum file, the connection and the first
  block all came before any progress was shown, which on a slow connection meant a long silent
  wait. The box now says "Connecting to the update server…" with a moving bar, then shows real
  progress (to a tenth of a megabyte) in small steps. The installer is also requested as a
  binary download rather than as JSON.
- **One email could create two requests** when the same day was written with and without a year
  (for example "12 Oct" in the subject and "12 October 2025" in the message). The day is now
  counted once, using the year that was written.
- **The box for editing a storage path was too thin** to read the path; it now fills the row.
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

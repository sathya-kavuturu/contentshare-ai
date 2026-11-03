# Build Prompt — Content Sharing Automation App

Copy everything below the line into a new Claude Code session opened in this folder.
Fill in the `<<...>>` placeholders first, or leave them and Claude will ask.

---

You are building **ContentShare**, a Windows 11 desktop app that automates how I share program content with departments in my organisation. Build it end to end, with tests and security checks. Work in phases, and show me a working result at the end of each phase before moving on.

## 1. What I do today (the process to automate)

1. Departments email me asking for specific program content, such as "please share the <program name> recording from <date>".
2. I read the email and work out which program they want.
3. I find that content on our storage server.
4. I copy it to that department's own storage location. Each department has a fixed destination.
5. When the copy has finished, I reply to the email saying the content has been shared.

Manual copies sometimes end up corrupted. TeraCopy helped because it verifies copies. The new app must never deliver a corrupted file.

## 2. Functional requirements

### 2.1 Gmail intake
- Connect to my Gmail with the **Gmail API and OAuth 2.0 (installed-app flow)**. Never ask for or store my password.
- Use the narrowest scopes that work: `gmail.readonly` to read, `gmail.send` to reply, and `gmail.labels`/`gmail.modify` only if needed to label processed mail. Explain each scope in the README.
- Check for new requests:
  - automatically every day at a time I set (default `<<09:00>>`), and
  - whenever I click **Check now** (also available from the system tray menu).
- Only look at mail from **known senders** in the department mapping (section 2.3). Ignore everything else, and log what was ignored.
- Never process the same email twice. Track Gmail message IDs in the local database and also add a Gmail label such as `ContentShare/Processed` or `ContentShare/Done`.

### 2.2 Understanding the request
- Use only the email's **plain-text** body. Never render HTML, load remote images or open attachments.
- Work out what content is wanted (program name, date or date range, language, episode or part, format) from the subject and body.
  - Default engine: deterministic rules (keywords, date parsing, configurable regex patterns) plus **fuzzy matching** (`rapidfuzz`) against the content index.
  - Optional engine, **off by default**, enabled in Settings: the Claude API (`claude-sonnet-5-5`) with structured JSON output to extract the fields. The LLM may only return extracted fields or pick from candidates the app already found. It must never produce file paths. Treat email text as untrusted input (prompt-injection safe).
- If one email asks for several items, create one request line per item.
- If the app is unsure (low match score, several close candidates, or nothing found), flag the request as **Needs review** and show the top 5 candidates so I can pick one or search by hand.

### 2.3 Department mapping (configuration)
- In-app settings, persisted to a config file (YAML or SQLite), editable through the UI:
  - Department name
  - Allowed sender email addresses and/or domains (one department can have several senders)
  - Destination root path (UNC paths such as `\\server\share\DeptA\Incoming` must work)
  - Optional subfolder pattern, such as `{program}/{date}`
  - Reply template for that department (falls back to a default)
- Initial departments: `<<list: Department | sender emails | destination path>>`
- Storage source roots to search: `<<\\storage-server\share\Programs ...>>`

### 2.4 Content search
- The storage server may be large and slow over the network, so keep a **local index** (SQLite with FTS5) of folder and file names, sizes and modified times under the source roots.
  - Update the index incrementally on a schedule (for example, before each daily check) and on demand.
  - Searches run against the index. Before copying, re-check the real path on disk.
- Results show the full path, total size, file count and last-modified date.

### 2.5 Review dialog (the main screen)
Show a review dashboard listing every pending request as a card or row with:
- Request number
- From (name and email), department, received time, subject, and an expandable plain-text snippet of the email
- **Content found at**: source path with size and file count, a confidence badge, and a **Change** button (candidate list or file browser)
- **Destination**: the resolved destination path (editable, but only inside the department's configured root)
- Checkbox: **Confirm copy**
- Checkbox: **Send reply automatically when the copy is verified** (default from settings)
- Actions: **Approve**, **Skip**, **Reject** (with an optional reply), and **Open email in Gmail**
- Bulk actions: **Approve all selected**, **Copy selected**

### 2.6 Copy and reply flow
- Nothing is ever copied without my explicit confirmation.
- After I approve, copies go into a queue with configurable concurrency (default 1–2 jobs). Show live progress per file and per job: bytes, speed, ETA and a verification stage. Pause, resume and cancel must work.
- When a job finishes and is verified:
  - If auto-reply was ticked, send the reply.
  - If not, show **Send reply** with an **editable preview** of the reply.
- Send the reply **in the same Gmail thread** (set `threadId`, `In-Reply-To` and `References`). Use a template with placeholders: `{requester_name}`, `{program}`, `{destination_path}`, `{file_count}`, `{total_size}`, `{date}`.
- If a copy fails or verification fails after retries, **never send the "shared" reply**. Mark the job failed and show the reason and a retry button.

### 2.7 History and audit
- History view: every request, decision, copy (source, destination, file list, hashes, duration, result) and reply sent, with search and filters. Export to CSV.
- Structured local logs with rotation. Never write OAuth tokens or full email bodies to logs.

### 2.8 Background behaviour
- Runs in the **system tray**, with an option to start at Windows login.
- When the scheduled check finds requests, show a **Windows toast notification** ("3 new content requests"). Clicking it opens the review screen.
- If the PC was off at the scheduled time, run the check at the next startup. Also provide an optional Windows Task Scheduler entry as a fallback.

## 3. Copy integrity (most important: no corrupted files)

Implement a verified copy engine that is at least as safe as TeraCopy's verify mode:

1. **Pre-flight checks**: the source exists and can be read, the destination root exists and can be written, there is enough free space (plus a margin), long paths are supported (`\\?\` and `\\?\UNC\` prefixes), and there are no name collisions. If a file already exists at the destination with the same hash, skip it. If it differs, ask me (never overwrite silently).
2. **Copy to a temporary name** such as `file.ext.cspartial` in the destination folder. Never write to the final name directly.
3. **Large chunked I/O** (for example 8–16 MB buffers). Hash the source **while reading it**, with a fast strong hash: **xxHash3-128 or BLAKE3** by default, SHA-256 as an option.
4. **Flush to disk**: call `flush()` and `os.fsync()` (`FlushFileBuffers`) before closing.
5. **Verify by reading the destination back** and hashing it independently. Read it unbuffered or with cache-bypass flags where possible, so the check reads the server's copy and not the local cache. The source and destination hashes must match.
6. **Detect source changes**: compare the source size and mtime before and after the copy. If either changed, re-copy.
7. **Atomic finalise**: rename the temp file to the final name only after verification passes. Keep the source timestamps.
8. **Retry** with backoff on network errors or a hash mismatch (default 3 attempts per file). Resume a job at file level after an interruption or crash, so files already verified are not copied again.
9. **Manifest**: write a checksum file (`.xxhash` / `.sha256sum` format) into the destination folder, and store the hashes in the database for the audit trail.
10. Clean up `*.cspartial` files left by failed or cancelled jobs.
11. **Never delete or move source files. Never delete anything at the destination** except the app's own temp files.
12. Document in the README why copies get corrupted (network drops, write caching, SMB issues, faulty RAM, cables or disks) and how this design prevents or detects each case. Recommend a hardware check if hash mismatches keep repeating on the same machine or path.

Benchmark this engine against `robocopy /J` and plain `shutil.copy2` on a large file set. Report throughput and confirm the overhead of verification is acceptable.

## 4. Technology choice

Pick the stack that best fits a Windows-only, single-user desktop tool with a polished UI. My suggested default, which you may change if you justify it:

- **Python 3.12+**
- **PySide6 (Qt 6)** with **PySide6-Fluent-Widgets**, so the app looks native on Windows 11 (Fluent / Mica, light and dark theme following the system)
- `google-api-python-client` and `google-auth-oauthlib` for Gmail
- **SQLite** (with FTS5) for the index, requests, jobs and audit
- `APScheduler` for scheduling, `keyring` (Windows Credential Manager / DPAPI) for the OAuth token, `xxhash` or `blake3` for hashing, `rapidfuzz` for matching, `pydantic` for config validation
- Package as a single signed-ready `.exe` with **PyInstaller or Nuitka**, plus an installer (Inno Setup)

Use a clean layered architecture: `gmail/` (intake and reply), `parsing/`, `index/`, `matching/`, `copy_engine/`, `jobs/` (queue and state machine), `config/`, `ui/`, `storage/` (database). The copy engine and the parser must be pure, testable modules with no UI or Gmail dependencies.

Request state machine: `NEW → NEEDS_REVIEW | READY → APPROVED → COPYING → VERIFIED → REPLIED`, plus the side states `SKIPPED`, `REJECTED` and `FAILED`.

## 5. Design and UX
- Calm, modern Fluent design with plenty of whitespace and a clear hierarchy. Every action is reachable in one or two clicks.
- Left navigation: **Requests** (default), **Active copies**, **History**, **Settings**.
- Status colours and icons (Ready, Needs review, Copying, Verified, Failed, Replied) that never rely on colour alone.
- Keyboard shortcuts, accessible labels, readable at 125–150% scaling, and correct on high-DPI screens.
- An empty state with a **Check now** button, and helpful error messages that say what to do next.
- A **first-run wizard**: connect Gmail, add source roots, add departments, set the schedule, then run a test check in **dry-run mode**.
- Show me UI mockups or screenshots at the end of the UI phase for feedback.

## 6. Security requirements
- OAuth tokens are stored only in Windows Credential Manager (via `keyring`). Never store them in plain files, and never commit secrets. Keep `client_secret.json` out of git.
- Least-privilege Gmail scopes (section 2.1), and a **Disconnect Gmail** option that revokes the token.
- Sender verification: accept an email only if the sender is allowlisted **and** the `Authentication-Results` header shows DMARC pass (or SPF and DKIM pass) for that sender's domain. Otherwise mark it **Suspicious** and require manual review.
- Path safety:
  - Resolve and normalise all paths.
  - Sources must sit under the configured source roots, and destinations under the department's destination root.
  - Block `..` traversal, symlinks or junctions that escape a root, reserved device names (`CON`, `NUL`…), and alternate data streams.
  - Program names taken from emails are sanitised before they are used in any subfolder name.
- Email content is untrusted: plain text only, length limits, no execution, no link following. If the LLM option is on, its output is schema-validated and can only choose from known candidates.
- No destructive file operations. Use a database-backed job journal so crashes cannot leave unclear states.
- Run `bandit`, `pip-audit`, `ruff` and `mypy --strict` (on core modules), plus secret scanning (`gitleaks`). Fix or explicitly justify every finding.
- Write a short `SECURITY.md` covering the threat model (spoofed sender, malicious email content, path injection, token theft, ransomware on shares, insider misuse) and the mitigations.

## 7. Testing requirements
- **pytest**, with at least 85% coverage on core modules (parser, matcher, config, copy engine, job state machine, Gmail adapter).
- Copy engine tests, using temp directories and fault injection:
  - bit flip in the destination → detected and re-copied
  - interrupted copy → resumes, and no partial file is left under the final name
  - source changes during the copy → detected
  - disk full, locked or in-use file, read-only destination, long paths (more than 260 characters), Unicode and emoji names, zero-byte files, very large files (sparse test files), and deep folder trees
  - path-traversal attempts are rejected (also property-based tests with `hypothesis`)
- Parser and matcher tests built from a fixture set of realistic sample emails. I'll provide redacted examples in `tests/fixtures/emails/`; create synthetic ones until then.
- Gmail adapter tests against a **fake Gmail service** (no real network in tests), covering threading headers, labels, pagination and auth errors.
- UI tests with `pytest-qt` for the main flows: review → approve → copy → reply.
- An end-to-end **dry-run mode** that runs the whole pipeline without copying or sending, and shows what would happen.
- A GitHub Actions workflow (`windows-latest`) running lint, type checks, tests and security scans.

## 8. Deliverables
- Source code, tests, `README.md`, `SECURITY.md` and `CHANGELOG.md`
- Step-by-step setup guide: creating the Google Cloud project and OAuth client (Desktop type; if our organisation uses Google Workspace, set it as an **Internal** app and note any admin approval needed), first-run wizard, and backup/restore of settings
- A built `.exe` and installer
- A short user guide with screenshots

## 9. How to work
1. Start by asking me any blocking questions: sample emails, folder naming conventions on the storage server, the department list, the reply wording, and typical file sizes and counts. Then propose the architecture and the UI wireframes.
2. Build in phases, each with passing tests before the next one starts:
   1. Config and database
   2. Verified copy engine, with benchmarks
   3. Content index and matching
   4. Gmail intake and reply, with the fake service first
   5. UI
   6. Scheduler, tray and notifications
   7. Packaging
   8. Security review and hardening
3. Keep a `PROGRESS.md` checklist up to date.
4. Never send a real email or copy real content during development unless I explicitly confirm it. Use dry-run mode and temp folders.

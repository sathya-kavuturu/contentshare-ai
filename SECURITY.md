# Security

ContentShare reads email from other people and copies files on shared storage, so it is
built to trust nothing that arrives by email and to do nothing without a person's click.
This page lists what could go wrong, what the program does about it, and what it cannot do.

Status: **reviewed by its author, tested by its own test suite, not independently audited and
not penetration-tested.** Treat the "Not covered" lists as real.

## What is protected, and from whom

| Asset | Who might want it |
|---|---|
| The storage archive (programs) | Anyone who could get a copy sent to them |
| Department folders | Another department, a stranger |
| The Gmail sign-in (token) | Malware, other users of the PC |
| The user's Windows sign-in hash | A remote server (via a booby-trapped email) |
| Email text and addresses | Log readers, anyone shoulder-surfing a notification |

## Threats and mitigations

### 1. Spoofed sender
*An attacker fakes the From line of an allowed address.*

* Only senders and domains listed in Settings are read. Everyone else's mail is recorded as
  ignored and left alone.
* A sender counts as **verified** only if Gmail's own `Authentication-Results` header shows
  DMARC pass (or SPF *and* DKIM) for the sender's domain, and the header was added by a trusted
  server (`mx.google.com`). Headers a sender writes themselves are not believed.
* Anything else is marked **Suspicious/unknown**, always goes to manual review, shows a warning,
  and needs an extra tick before a copy can start. The reply goes to the From address only,
  never to `Reply-To`.
* Allowing a whole public mail domain (`gmail.com`, `outlook.com`, …) is refused by the settings:
  anyone can own an address there, and verification would pass for them.

*Not covered:* a genuine account that was taken over passes verification. The person confirming
each copy is the last defence; there is no content-based anomaly detection.

### 2. Malicious email content
*The text or the sender name tries to run something, change the program's behaviour, or reach a
server.*

* Only plain text is read. HTML is reduced to its words (nothing rendered or fetched); attachments
  are never opened; links are never followed; quoted replies, signatures and addresses are
  dropped. Subject 300 characters, body 20,000, lines 500, 20 request lines per email.
* Email text is **never** a file path: the destination folder is named from the content's own name
  on the server, not from the email.
* **Plain text on screen.** Qt shows text that looks like HTML as HTML, and HTML can load an image
  from `\\attacker\share`, which makes Windows send the user's sign-in hash to that server. Every
  label and message box uses `Label` / `SafeMessageBox` (`ui/widgets.py`), which never interpret
  text as HTML; a test fails if a screen imports Qt's default ones.
* Pop-up notifications show counts and request numbers only.
* CSV export defuses spreadsheet formulas in text that came from emails (`=cmd|...`).
* Search uses only letters and digits from the email, quoted, so it cannot inject search syntax;
  all SQL is parameterised.
* Replies are built from a fixed template with fixed placeholders (not `str.format`), and the
  email library refuses line breaks in headers, so a crafted name or subject cannot add a
  `Bcc`.
* Parsing time is bounded (hostile 1 MB bodies, 2 MB subjects and 50,000-line lists parse in
  milliseconds; tested).
* The optional "use an AI model to read emails" engine from the specification is **not built**,
  so no email text leaves the PC.

### 3. Path injection
*A request tries to read or write somewhere it should not.*

* Sources must sit under the configured storage folders; destinations under the department's folder.
  The one exception is a department with **Allow choosing from anywhere** switched on (off by
  default): the person can pick a folder or file anywhere with the file dialog. That path comes
  only from the dialog, never from an email; it must be absolute, is resolved like any other, is
  refused inside Windows or Program Files, as a whole drive or share, or when it overlaps the
  department's destination, and it is checked again at approval and just before the copy.
  Paths are normalised and symlinks/junctions resolved before the check, so a link cannot lead out.
* Blocked: `..`, absolute paths in names, reserved device names (`CON`, `NUL`, …), alternate data
  streams (`name:stream`), trailing dots/spaces, illegal characters, over-long names, names that
  differ only by case. Links and junctions inside a source folder are skipped, not followed.
* The path is **checked again immediately before the copy**, on the live disk, not on the stored
  index; source and destination may not overlap.
* The settings refuse a copy folder that is the archive, inside it, or around it; two departments
  with the same or nested folders; and folders inside the Windows or Program Files folders.

### 4. Token theft
*Someone obtains the Gmail access.*

* The token lives only in **Windows Credential Manager** (per Windows user). It is never written to
  a file, the database or the log. `client_secret.json` and token files are in `.gitignore`.
* Least privilege: read, send and (optionally) label. **Settings → Gmail → Disconnect** revokes
  the token at Google and deletes the local copy.
* Logs never contain tokens or email text. Log files use a formatter that prints, for an error,
  only its type and where it happened, never its message, because library error messages
  sometimes quote the data they failed on.

*Not covered:* other programs running as the **same Windows user** can ask Credential Manager for
the token. If malware runs as the user, the token, settings and archive access are all exposed.
Use a separate, least-privilege Windows account for this program if that matters.

### 5. Ransomware and damage on shares
*Malware or a mistake destroys or alters content.*

* **The program never deletes, renames or edits anything on the storage server**, and the only
  things it deletes anywhere are its own temporary files: `*.cspartial` copies in progress (the
  engine's single delete function refuses any other name; tested) and the temporary file used
  while saving settings. A different file already at the destination
  is reported as a conflict and is replaced only if the user approves that exact file.
* Every file is copied under a temporary name, read back uncached and compared with a hash of
  the source, and only then renamed into place. The job journal is written to the database
  before each step, so a crash leaves a clear state.

*Not covered:* if the **source** is already encrypted or corrupted, a perfect copy of it is
still a bad copy: verification proves the copy equals the source, not that the source is
good. Keep ordinary backups/snapshots of the archive, and give the account running this
program read-only access to the archive and write access only to department folders.

### 6. Insider misuse
*A person with access to the app sends content to the wrong place or hides what they did.*

* Nothing is copied or sent without an explicit click (**Confirm copy**, **Approve**; replies
  can be previewed and edited). A "shared" reply can only follow a **verified** copy.
* A request can only go to its own department's folder. The *Change* button accepts only a
  folder inside that department's folder; it is checked again before copying.
* Every state change is recorded in an append-only audit table (database triggers refuse edits
  and deletes) and can be exported to CSV.

*Not covered:* the audit log is not tamper-proof against someone who can edit the database file
directly, and the program does not separate roles: whoever can open it can approve. Restrict
who can use the PC account.

### 7. Local files and the installed program
* Settings, the database and logs live in the user's `%APPDATA%\ContentShare`, protected by
  Windows' normal per-user permissions. The installer needs no administrator rights and installs
  into the user's own folder, so, as with any per-user program, a process running as that user
  can replace it.
* The "show window"/"quit" channel between a second launch (or the installer) and the running
  copy is a named pipe only the current user can open, and no data is read from it.
* The built program is **not code-signed** (no certificate was available), so Windows
  SmartScreen will warn. The build can sign (`docs/PACKAGING.md`); until then publish
  the installer's SHA-256 checksum next to it.

### 8. The built-in updater
At start-up (switchable in Settings → General) the program reads the project's public GitHub
releases page and, only when the user clicks **Download** and then **Update**, fetches and runs
the new installer. `app/updates.py` limits what this can do:
* Only `https://` addresses are used (a redirect to anything else is refused), and downloads are
  accepted only from this project's own release address.
* The installer must be named like a ContentShare installer, match the release's version, be
  under 400 MB, and match the SHA-256 in the `SHA256SUMS.txt` published in the same release.
  A damaged or cut-off download is deleted. Only installers the program downloaded itself are
  ever started, and only after the user clicks Update; it waits while copies are running.
* Nothing about the PC, the settings or the mail is sent: it is a plain GET of a public page.
* **Limit:** the checksum comes from the same place as the installer, so someone who takes
  over the GitHub account (or its release workflow) can publish a malicious "update" that passes
  the check. Protect the account with two-factor sign-in and, when a code-signing certificate is
  available, have the updater verify the installer's signature as well. A file downloaded by
  the program has no "downloaded from the internet" mark, so SmartScreen does not warn; the
  checksum is the only gate.

## What the Phase 8 review found and fixed

| # | Finding | Severity | Fix |
|---|---|---|---|
| 1 | Qt labels and message boxes rendered email-derived text as HTML (image from a network path leaks the Windows sign-in hash) | **High** | All text is plain (`ui/widgets.py`); test scans every screen |
| 2 | A public-mail domain (`gmail.com`) could be allowlisted, opening the app to anyone | High | Rejected by the settings |
| 3 | Copy folder could overlap the archive or another department's folder | Medium | Rejected by the settings; the job still checks at copy time |
| 4 | Copy folder could be in the Windows or Program Files folders | Low | Rejected by the settings |
| 5 | Log files could contain text quoted in library error messages | Medium | Messages are never logged; types and locations only |
| 6 | The parser's subject handling had a quadratic step, safe only because another module cut the subject first | Low | Parser cuts it itself |
| 7 | The engine's delete helper trusted its callers | Low | Refuses any name that is not `*.cspartial` |
| 8 | `.gitignore` did not cover signing certificates (`*.pfx`) | Low | Added |

## Checks run

`ruff`, `mypy --strict`, `bandit` (no findings; the two intentional `subprocess` uses call a
fixed Windows program with list arguments), `pip-audit` (no known vulnerabilities at the time of
writing), a pattern scan of the whole tree for keys and tokens (nothing found), and the
test suite, which includes fault injection, hostile-input and property-based tests.

**Not run here:** `gitleaks` (it runs in CI; this project is not in git yet), any dynamic or
penetration test, a test against a real Gmail mailbox, a real NAS or real Authentication-Results
headers. CI actions are referenced by version tag, not pinned to a commit hash; pin them when
publishing.

## Running it safely

1. Create the Google Cloud project as an **Internal** app in your own Workspace (see
   `docs/GMAIL_SETUP.md`); keep `client_secret.json` out of shared folders and out of git.
2. Run the program as a normal user, not an administrator.
3. List specific sender addresses; avoid wide domains.
4. Start in **dry-run mode** and read the first requests carefully.
5. Keep the archive snapshotted/backed up; give this account read-only access to it.
6. Install only an installer whose checksum matches the one you published.

## Reporting a problem

Tell the person who maintains this installation. Include the Windows version, the program
version (`ContentShare.exe --version`) and the relevant lines of
`%APPDATA%\ContentShare\logs\contentshare.log` (they contain no email text). Do not send the
settings file, the database or `client_secret.json`.

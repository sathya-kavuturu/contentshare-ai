# ContentShare

A Windows app that turns department content-request emails into verified copies and replies in
the same email thread. **Nothing is copied or sent until you click Approve.**

1. A department emails the name of a program.
2. ContentShare finds it on the storage server.
3. You check it and approve.
4. It copies the content to that department's folder, proves every file arrived intact, and
   replies to the sender.

> Status: built and tested against invented data and a fake Gmail. **It has not yet been used with
> a real mailbox or storage server**, so start in dry-run mode (see below).

## Install

Download `ContentShare-Setup-<version>.exe` from the
[Releases](https://github.com/sathya-kavuturu/content-sharing/releases/latest) page and run it.
It installs for the current user only and needs no administrator password. The installer is not
code-signed, so Windows may warn: choose **More info → Run anyway**, and check the file against
`SHA256SUMS.txt` from the same release.

The step-by-step guide with pictures is [docs/USER_GUIDE.md](docs/USER_GUIDE.md). The same help is
built into the app (**Help** in the menu) and works offline.

## First steps

1. The first start asks where the programs are stored and for your first department (sender
   emails and the folder its copies go to).
2. Connect Gmail in **Settings → Gmail** ([docs/GMAIL_SETUP.md](docs/GMAIL_SETUP.md)).
3. Every time ContentShare opens it asks you to **sign in**: choose your name and type your
   password, or create a new user (any password will do). The History shows your name beside
   what you approve.
4. Leave **Dry-run mode** on at first: Approve then only reports what would be copied.

## What it does

* **Requests.** One card per email, with a section for each folder asked for. A folder is
  **Ready** (found), or **Needs review** (not found, found in several places, or unclear) with the
  reason and the best matches to choose from. Nothing is skipped quietly: a listed name with no
  folder shows as Needs review, and email lines that did not become a request are listed on the
  card. A copy starts only after you tick *Confirm copy* and click Approve. A sender Gmail could
  not verify needs one more tick.
* **Finding the content.** ContentShare keeps a local index of folder and file *names* on the
  storage server. It looks for a folder with exactly the name in the email first (a date range
  written before the name, such as `Nov 22-28`, is ignored; a date inside the name is kept), then
  a file with that name, then similar names. A department can be set to *The emails give exact
  folder names*, which never guesses. If a name is not found and the index is more than 10
  minutes old, the storage is read again before giving up. A single file is copied straight into
  the department folder, without an extra folder around it.
* **Active copies.** Progress, speed, time left, pause, resume and cancel. Cancelling keeps the
  files already verified, and a copy can be retried. After a crash or restart it continues where
  it stopped.
* **Replies.** One reply per email, naming every folder that was copied and checked, sent only to
  the person who asked, in their thread. A "shared" reply is impossible for a copy that was not
  verified.
* **History.** Every request with who approved it, how long the copy took, and a step-by-step
  audit trail. **Export CSV** saves it for Excel.
* **By itself.** Checks Gmail on a schedule and adds cards. That is all it does alone. It can
  start with Windows, stay in the tray, show pop-ups and send a Telegram message to a group. Both
  show **counts only**, never email text. Looks for a newer version when it starts and installs
  it only when you press Update.
* **Never destructive.** The app never deletes or changes anything on the storage server.

## Copy integrity: how a corrupted file is prevented

A file is only given its final name after the bytes **on the destination** were read back and
matched the bytes read from the **source**. Per file:

1. **Pre-flight**, before any byte moves: source readable, destination writable, enough free
   space (plus a margin), no names Windows cannot store (reserved names, trailing dots), no
   names that differ only by case, paths within Windows limits (long paths use `\\?\`).
2. **Copy to `name.ext.cspartial`** in 8 MB chunks, hashing the source *as it is read*
   (xxHash3-128 by default; BLAKE3 or SHA-256 selectable).
3. **`fsync`** so the data is on the disk, not just in the OS write cache.
4. **Read the copy back** with `FILE_FLAG_NO_BUFFERING`, which bypasses the Windows file cache,
   and hash it independently. If the two hashes differ, the file is discarded and copied again.
5. **Check the source did not change** (size and modified time before vs after). If it did,
   the file is copied again.
6. **Atomic rename** to the final name. On Windows this refuses to replace an existing file,
   so a file that appeared meanwhile is never silently overwritten.
7. Timestamps are preserved. If you tick *Keep a checksum file in each copied folder*
   (Settings → Matching and copying), a `<folder>.checksums.xxhash` (or `.sha256sum` / `.b3sum`)
   is written next to the files and can be re-checked with `xxhsum -c`, `sha256sum -c` or
   `b3sum --check`.

Retries use exponential backoff (default 3 attempts per file). If a file still fails, the request
is marked **Failed** with the reason and a Retry button, and the "shared" reply can never be sent
for it. Every verified file is journaled in SQLite, so after a crash or cancel only unfinished
files are copied again. Links and junctions inside a source folder are skipped, not followed.

| Cause | What ContentShare does |
|---|---|
| Network drop or SMB timeout mid-copy | Size and hash mismatch, or an I/O error: the file is discarded and retried |
| Write caching (data "written" but only in RAM) | `fsync` before verifying; temp name until verified, so a half-written file never has the final name |
| Reading back the OS cache | Read-back uses unbuffered I/O, so it reads what is actually stored |
| Source modified or still being written | Size and time checked before and after; a file that keeps changing fails |
| Faulty RAM, disk, cable or NIC | Caught by the independent read-back hash. **If mismatches keep repeating on one machine or path, test the hardware** instead of relying on retries |
| Antivirus or backup tool locking a file | Lock errors are retried with backoff, then reported per file; other files still copy |
| Disk full | Free space is checked first; a full disk fails fast and removes the temp file |

**Limit to know.** A NAS may still answer the read-back from its own cache. Verification proves
the data came back consistent through the network path, not that the platters hold it.

## Security in short

* Only mail from the senders and domains in a department is read. A sender counts as verified
  only if Gmail's own `Authentication-Results` shows DMARC (or SPF and DKIM) passing; otherwise
  the request goes to manual review.
* Replies go only to the address the request came from (never a `Reply-To`).
* Text from an email is never used as a file path, and every path must stay inside the configured
  folders.
* The Gmail token and the Telegram bot token live only in Windows Credential Manager. Tokens and
  email text are never logged.

Details: [SECURITY.md](SECURITY.md).

## Development

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m contentshare        # run the app
.\.venv\Scripts\python -m pytest              # fast tests (each test stops after 5 minutes)
.\.venv\Scripts\python -m pytest -m slow      # also copies a >4 GiB file
.\.venv\Scripts\python -m ruff check src tests packaging
.\.venv\Scripts\python -m mypy                # strict
```

Every push runs lint, strict types, tests with coverage (85% minimum), bandit, pip-audit and a
secrets scan. A push to `main` that passes everything builds the installer and publishes a
GitHub Release automatically. To build locally: `python packaging/build.py`
([docs/PACKAGING.md](docs/PACKAGING.md)). To refresh the guide's pictures:
`python tools/make_screenshots.py`.

## More

* [docs/USER_GUIDE.md](docs/USER_GUIDE.md): using the app, with pictures
* [docs/GMAIL_SETUP.md](docs/GMAIL_SETUP.md): connecting Gmail
* [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): how it is built
* [docs/PACKAGING.md](docs/PACKAGING.md): building the program and the installer
* [docs/BENCHMARKS.md](docs/BENCHMARKS.md): copy speed (`python benchmarks/bench_copy.py`)
* [SECURITY.md](SECURITY.md) and [CHANGELOG.md](CHANGELOG.md)

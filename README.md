# ContentShare AI

A Windows app that turns content-request emails into verified copies and replies in the same
email thread. **Nothing is copied or sent until you click Approve.**

ContentShare AI is [ContentShare](https://github.com/sathya-kavuturu/content-sharing) plus
**smart reading**: it can also find requests from senders who are in no department, using a small
open-source language model that runs on your own PC. It installs beside the original ContentShare
(its own settings, history and Gmail sign-in), so you can try both.

1. A department emails the name of a program.
2. ContentShare finds it on the storage server.
3. You check it and approve.
4. It copies the content to that department's folder, proves every file arrived intact, and
   replies to the sender.

> Status: built and tested against invented data and a fake Gmail. **It has not yet been used with
> a real mailbox or storage server**, so start in dry-run mode (see below). Smart reading is
> described, with its limits, in [its own section](#smart-reading-requests-from-unknown-senders).
> It was run end to end with the real Qwen3 1.7B model and the real llama.cpp download (3 to 8
> seconds per email on a CPU), but not yet with the recommended 4B model or on a graphics card,
> and not on real emails.

## Install

Download `ContentShare-Setup-<version>.exe` from the
[Releases](https://github.com/sathya-kavuturu/contentshare-ai/releases/latest) page and run it.
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

## Smart reading: requests from unknown senders

Without it, only mail from the senders you list in a department is read. Turn on **Settings →
Smart reading** and ContentShare AI also reads recent mail in your main inbox from anyone else
(mail you sent and automatic senders such as no-reply are left out), works out whether it asks for
content, and pulls out the folder names.

**How an email is read**

1. *Rules first, no model.* If lines of the email are exactly the names of folders on the storage
   server, it is a request.
2. *The model.* Otherwise, if the email contains words that ask for something, a small language
   model answers in a fixed JSON shape: is it a request, and which names does it give? Names that
   are not really written in the email are thrown away, as are names that look like paths or
   patterns. Newsletters, invoices and questions are remembered as "not a request".
3. *Look up.* Every name is searched on the storage server.
4. *You decide.* These requests always wait for you: press **Choose department…**, check the
   names (a note says they were read by the smart reader), choose the folders, approve. Nothing is
   ready, copied or answered by itself.

**Switches.** *Settings → Smart reading → Use the local model* is the master switch: off (the
default) means the model is never started. A second tick, *Also read emails from senders that are
not in any department*, controls the stranger mail. Each department also has a tick, *Also read
this department's emails with the local model*: when the emails of a department have no lines
that are exactly folder names, the model reads them too. **Show the prompt…** shows exactly what
the model is sent. **Start the model now / Stop the model**
controls it by hand; otherwise it starts when mail needs reading and stops after the idle time.
Every answer of the model (a request or not, the folders, the confidence) is written to the
History of the request.

**Safety.** Mail from strangers is untrusted input. The model has no tools: it cannot copy, send or
open anything, and its answer is only ever used as search text. Everything runs on this PC; email
text is never sent to an AI service. The program that runs the model listens only on `127.0.0.1`
and needs a random key.

**The model.** Recommended: **Qwen3 4B** (`Qwen3-4B-Q4_K_M.gguf`, about 2.5 GB, Apache License 2.0).
Smaller option: **Qwen3 1.7B** (`Qwen3-1.7B-Q8_0.gguf`, about 1.8 GB, Apache License 2.0). It is
run by **llama.cpp** (build `b12086`, MIT licence). Only licences that allow use inside an
organisation (Apache-2.0, MIT) are used; models with research-only or community licences are left
out. The files are downloaded from inside the app (**Download…**) and each one is checked against
a SHA-256 pinned in the program before it is used.

**Your PC.** 8 GB of memory or more (the model needs about 3 GB while it reads and is stopped again
after 10 idle minutes), about 3 GB of disk, no graphics card needed. A graphics card with 2 GB or
more makes it faster (tick **Use the graphics card**; it uses llama.cpp's Vulkan build).

**Gmail cost and limits.** Google states that standard use of the Gmail API has no charge. The
limits are 6,000 quota units per user per minute and an 80,000,000-unit daily threshold;
listing mail costs 5 units and reading a message 20 (60 when the full message is read, as here). A
check every 15 minutes (96 a day) reading even 50 new messages each time stays far below both. Google
says charges for going above the quota are planned for later in 2026.

**Learning.** What you approve, skip or reject is kept on this PC. When the model reads a
similar email later, the most similar earlier ones are shown to it as examples. This changes its
answers at once; nothing is trained. **Forget what it learned** deletes them. With enough
decisions saved (a few hundred), a small classifier could be trained to decide which emails are
worth the model's time: that is not built yet.

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

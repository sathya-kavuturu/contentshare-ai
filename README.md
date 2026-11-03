# ContentShare

Turns department content-request emails into verified copies and in-thread replies.
Nothing is copied or sent without your explicit confirmation.

> Status: all 8 phases built (config, database, verified copy engine, content index and
> matching, Gmail intake and replies, the screens, daily check, tray and notifications, Windows
> build and installer, **security review**). Developed against invented data and a fake Gmail:
> **it has not yet been used with a real mailbox or storage server.** See [SECURITY.md](SECURITY.md)
> and [CHANGELOG.md](CHANGELOG.md). See [PROGRESS.md](PROGRESS.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Running it

```powershell
.\.venv\Scripts\python -m contentshare
```

The first start shows a short setup (storage folder, first department). Then connect Gmail in
Settings (see [docs/GMAIL_SETUP.md](docs/GMAIL_SETUP.md)) and refresh the index. **Start in
dry-run mode**: Approve then only reports what would be copied.

* **Requests**: every request line is a card. Ready ones show what was found and where it
  would go; ones that need you list the best matches. A copy starts only after you tick
  *Confirm copy* and click Approve. A sender Gmail could not verify needs one more tick.
* **Active copies**: progress, speed, time left, pause / resume / cancel. Cancelling keeps files
  that were already verified, and the copy can be retried.
* **History**: every request, its audit trail, and CSV export.
* **Settings**: departments, storage folders, Gmail, matching strictness, checksum type.
* **Every day** (Settings → General, default 09:00) the app checks Gmail by itself and adds
  request cards. That is all it does by itself: **nothing is approved, copied or sent without
  your click**. If the PC was off or asleep at that time, it checks once when it is next on.
* **Tray.** Closing the window keeps ContentShare running in the notification area (change
  this in Settings). Use the tray icon's menu to quit. Pop-ups show counts only, never
  email text, so they are safe on a locked screen or while sharing your screen.
* **Start with Windows** is a tick box in Settings → General; it starts the app hidden in the
  tray. You can also turn it off in Windows Settings → Apps → Startup.
* The app never deletes or changes anything on the storage server. If you quit while copies
  run, they continue from where they stopped the next time it starts.

## Building the Windows program

`python packaging/build.py` builds `dist/ContentShare/ContentShare.exe` and proves it works with
its own self-test; `installer/contentshare.iss` turns it into a setup file. See
[docs/PACKAGING.md](docs/PACKAGING.md). Users: [docs/USER_GUIDE.md](docs/USER_GUIDE.md).

## Development

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest            # fast tests
.\.venv\Scripts\python -m pytest -m slow    # also copies a >4 GiB file
.\.venv\Scripts\python -m ruff check src tests
.\.venv\Scripts\python -m mypy              # strict
```

## Copy integrity: how a corrupted file is prevented

A file is only given its final name after the bytes **on the destination** were read back and
matched the bytes read from the **source**. The steps, per file:

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
7. Timestamps are preserved. After all files pass, a checksum file
   (`<folder>.checksums.xxhash`, or `.sha256sum` / `.b3sum`) is written next to them. You can
   re-check it any time with `xxhsum -c`, `sha256sum -c` or `b3sum --check`.

Retries use exponential backoff (default 3 attempts per file). If a file still fails, the job is
marked **Failed** with the reason and a Retry button, and the "content shared" reply can never be
sent for it (the state machine has no path from Failed to Verified or Replied).

**Resume.** Every verified file is journaled in SQLite as soon as it finishes. After a crash or
cancel, only unfinished files are copied again; verified files are skipped if their size and
modification time still match, and anything else is compared by hash first.

**Never destructive.** The app never deletes or moves source files, and never deletes anything
at the destination except its own `*.cspartial` temp files. A different file already at the
destination is reported as a **conflict**; it is replaced only for files you explicitly approve.
Links and junctions inside a source folder are skipped, not followed.

### Why copies get corrupted, and what catches each cause

| Cause | What happens | What ContentShare does |
|---|---|---|
| Network drop or SMB timeout mid-copy | Short or partial file, sometimes reported as success | Size and hash mismatch, or an I/O error → file discarded and retried |
| Write caching (data "written" but only in RAM) | Power loss or disconnect leaves a damaged file | `fsync` before verifying; temp name until verified, so a half-written file never has the final name |
| Reading back the OS cache | A copy can look fine because you are comparing the cache with itself | Read-back uses unbuffered I/O, so it reads what is actually stored |
| Source modified or still being written | Copy mixes old and new content | Size/mtime checked before and after; mismatch → retry, and a file that keeps changing fails |
| Faulty RAM, disk, cable or NIC | Occasional flipped bits | Detected by the independent read-back hash. **If mismatches keep repeating on the same machine or path, stop and test the hardware** (memory test, disk SMART, cable/switch) rather than relying on retries |
| Antivirus or backup tool locking a file | Copy fails or is truncated | Lock errors are retried with backoff, then reported per file; other files still copy |
| Disk full | Truncated file | Free space checked up front; a full disk fails fast and removes the temp file |

**Limits to know.** Unbuffered read-back asks the server for the data again, but a NAS or
storage controller may still serve it from its own cache. Verification proves the data came back
consistent through the network path, not that the platters hold it. Periodic re-checks of the
checksum files protect against later bit rot.

## Finding the content (Phase 3)

1. **Index.** The storage server is scanned into a local SQLite index of folder and file *names*,
   sizes and dates (never contents). Searching is instant; the real path is re-checked on disk
   before a copy. Scans only read. A failed or cancelled scan never removes index entries, and a
   scan lists every folder, because a folder's modified time does not change when something is
   added several levels below it.
2. **Reading the email.** Only the plain text is used (HTML is reduced to its words; quoted
   replies, signatures, links and addresses are dropped). Rules extract program words, dates
   (`12 Oct 2026`, `12/10/2026`, `12-14 Oct`, `yesterday`, `last Sunday`), language, part or episode
   (`Day 2`) and format. A list, or several dates, becomes one request line each (maximum 20).
   Nothing from the email is ever used as a file path.
3. **Matching.** Candidates come from the index (steered by the requested date, so a program that
   recurs hundreds of times is still found) and are scored 0-100: name similarity first, then
   bonuses for a matching date, language, part and format, and heavy penalties for a different one.
4. **Decision.** A request becomes **READY** only if the best match is clearly the best: the name
   itself is a strong match, nothing differs from the request, and no other candidate is close.
   Everything else is **NEEDS_REVIEW** with up to 5 options and the reason. Requests from an
   unverified sender, or one not in any department, always go to review, and review items are
   never pre-selected.

Tune it in the settings file: `parsing` (day-first dates, languages and their short codes, extra
program patterns, extra filler words), `index` (excluded names, folders only) and `matching`
(`auto_ready_score`, `ambiguity_gap`).

## Gmail (Phase 4)

Setup, with the Google Cloud steps and what each permission is for, is in
[docs/GMAIL_SETUP.md](docs/GMAIL_SETUP.md).

* **Reading requests.** Only mail from the senders and domains in the department list is ever
  asked for, and each message is checked again after it is read. Each message is processed once
  (its Gmail id is recorded in the database, and a `ContentShare/Processed` label is added if
  labels are on). Mail from anyone else is recorded as ignored and otherwise left untouched.
* **Who is trusted.** A sender counts as verified only if Gmail's own `Authentication-Results`
  shows DMARC (or SPF and DKIM) passing for the sender's domain. Otherwise the request is
  marked suspicious or unknown and goes to manual review, never straight to Ready.
* **Replies.** The reply goes only to the address the request came from (never a `Reply-To`),
  in the same Gmail thread (`threadId`, `In-Reply-To`, `References`). A "content shared" reply
  is possible only for a request whose copy was verified. It is journaled before sending and a
  database rule allows only one per request; if the connection drops mid-send, the app looks in
  the Gmail thread for the reply's tag instead of guessing, so it is never sent twice.
* **Your token** lives only in Windows Credential Manager, and `gmail-disconnect` revokes it.
* **Not logged:** tokens and email bodies. The audit trail records ids, states and reasons.
* Replies are per request line: an email asking for three items gets up to three replies.

## Benchmarks

See [docs/BENCHMARKS.md](docs/BENCHMARKS.md). Reproduce with `python benchmarks/bench_copy.py`.

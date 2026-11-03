# Copy benchmarks

Measured 2026-11-03 on the development PC (Windows 11, NTFS, Python 3.14.5). Source and
destination are on the **same local disk**, so this shows relative cost, not network speed.
Dataset: 301 files, 1.84 GiB (one 1.5 GiB file + 300 clips of 0.25–2 MB, random data). Best of 2 runs.

| Method | Verified | Best time | Throughput |
|---|---|---:|---:|
| shutil.copy2 | no | 1.1 s | 1,680 MB/s (cache artefact, see below) |
| robocopy /J (unbuffered) | no | 14.9 s | 126 MB/s |
| shutil.copy2 + hash both sides | separate pass | 11.1 s | 169 MB/s (not flushed, see below) |
| **ContentShare, xxh3_128 (default)** | **yes** | **13.1 s** | **144 MB/s** |
| ContentShare, blake3 | yes | 14.7 s | 129 MB/s |
| ContentShare, sha256 | yes | 15.7 s | 120 MB/s |

## How to read this

- **`shutil.copy2` is not a real baseline.** 1,680 MB/s is the Windows file cache accepting the
  data; the function returns before it reaches the disk. A file copied this way can still be
  incomplete on disk when "done" is reported, which is one of the corruption causes this app
  exists to prevent.
- **`robocopy /J` is the fair baseline.** It writes through to disk and gives the machine's real
  sustained rate, about 126 MB/s here. It does not verify anything.
- **The engine copies, forces the data to disk, then reads every file back, and still matched
  `robocopy /J`** (144 vs 126 MB/s). On this machine, verification is not a bottleneck: the disk is.
- **The "shutil + hash both" row is optimistic.** It skips the `fsync` the engine does, so part of
  its write is still in cache when it ends.
- **Hash choice.** xxHash3 is fastest and is the default. BLAKE3 and SHA-256 cost roughly 12% and
  20% more total time here (CPU-bound hashing on top of a disk-bound copy). Use SHA-256 only if
  you need a checksum file a standard tool can verify anywhere.

## What this does not tell you

Verification needs a second read of every byte. Locally that is a disk read; over a network share
it is a second trip over the network, so total time can approach 2x a plain copy when the network
is the limit. That is the price of detecting corruption. Run the benchmark against your real
storage to get the number that matters:

```powershell
python benchmarks\bench_copy.py --large-gib 2 --small-count 200 --dest-dir \\nas02\media\bench
```

(`--dest-dir` is where the copies go; the test set is created locally and everything is deleted
afterwards. Use a folder you do not mind being written to.)

"""Benchmark the verified copy engine against robocopy /J and shutil.copy2.

    python benchmarks/bench_copy.py --large-gib 1.5 --small-count 300 --repeat 2 \
        --markdown docs/BENCHMARKS.md

Methods
  shutil.copy2        plain copy, no verification
  robocopy /J         Windows unbuffered copy, no verification
  shutil + verify     shutil.copy2, then hash source and destination (a manual "copy, then check")
  ContentShare        this engine: hash while copying, fsync, uncached read-back, atomic rename

Everything runs on one disk here, so the OS file cache flatters the methods that do not bypass
it. On a real network share the gap between "unverified" and "verified" is usually smaller,
because the network is the bottleneck, not the hash.
"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from contentshare.copy_engine import MIB, Control, CopyOptions, build_plan, copy_tree
from contentshare.copy_engine.winio import hash_file


def make_dataset(root: Path, large_gib: float, small_count: int, seed: int = 1) -> int:
    rng = random.Random(seed)  # noqa: S311 - sizes only; content is os.urandom
    root.mkdir(parents=True)
    total = 0

    def write_random(path: Path, size: int) -> None:
        with path.open("wb") as handle:
            left = size
            while left:
                n = min(left, 8 * MIB)
                handle.write(os.urandom(n))
                left -= n

    big = int(large_gib * 1024**3)
    if big:
        write_random(root / "master_recording.mxf", big)
        total += big
    for i in range(small_count):
        size = rng.randint(256 * 1024, 2 * MIB)
        folder = root / f"part_{i % 10}"
        folder.mkdir(exist_ok=True)
        write_random(folder / f"clip_{i:04d}.mp4", size)
        total += size
    return total


def tree_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def run_shutil(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, copy_function=shutil.copy2)


def run_robocopy(src: Path, dst: Path) -> None:
    cmd = ["robocopy", str(src), str(dst), "/E", "/J", "/R:0", "/W:0",
           "/NFL", "/NDL", "/NJH", "/NJS", "/NP"]  # fmt: skip
    code = subprocess.run(cmd, capture_output=True, check=False).returncode  # noqa: S603
    if code >= 8:
        raise RuntimeError(f"robocopy failed with exit code {code}")


def run_shutil_verify(src: Path, dst: Path) -> None:
    run_shutil(src, dst)
    control = Control()
    for source in tree_files(src):
        target = dst / source.relative_to(src)
        a, _ = hash_file(str(source), "xxh3_128", 8 * MIB, control, bypass_cache=False)
        b, _ = hash_file(str(target), "xxh3_128", 8 * MIB, control)
        if a != b:
            raise RuntimeError(f"verification failed for {source}")


def run_engine(algorithm: str) -> Callable[[Path, Path], None]:
    def run(src: Path, dst: Path) -> None:
        result = copy_tree(
            build_plan(src, dst), CopyOptions(algorithm=algorithm, chunk_bytes=8 * MIB)
        )
        if not result.ok:
            raise RuntimeError(f"engine failed: {result.failures}")

    return run


def time_method(
    name: str, fn: Callable[[Path, Path], None], src: Path, work: Path, repeat: int
) -> list[float]:
    times: list[float] = []
    for i in range(repeat):
        dst = work / f"{name.replace(' ', '_').replace('/', '')}_{i}"
        start = time.perf_counter()
        fn(src, dst)
        times.append(time.perf_counter() - start)
        shutil.rmtree(dst, ignore_errors=True)
        print(f"  {name:<28} run {i + 1}: {times[-1]:6.1f} s", flush=True)
    return times


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--large-gib", type=float, default=1.5)
    parser.add_argument("--small-count", type=int, default=300)
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--workdir", type=Path, default=None, help="where the source set is made")
    parser.add_argument(
        "--dest-dir", type=Path, default=None, help="folder (local or network share) to copy into"
    )
    args = parser.parse_args()

    work = Path(tempfile.mkdtemp(prefix="cs-bench-", dir=args.workdir))
    try:
        print(f"Creating dataset in {work} ...", flush=True)
        src = work / "src"
        dest_root = args.dest_dir or work
        dest_root.mkdir(parents=True, exist_ok=True)
        total = make_dataset(src, args.large_gib, args.small_count)
        files = len(tree_files(src))
        gib = total / 1024**3
        print(f"Dataset: {files} files, {gib:.2f} GiB\n", flush=True)

        methods: list[tuple[str, Callable[[Path, Path], None], str]] = [
            ("shutil.copy2", run_shutil, "no"),
            ("robocopy /J", run_robocopy, "no"),
            ("shutil.copy2 + hash both", run_shutil_verify, "yes (separate pass)"),
            ("ContentShare (xxh3_128)", run_engine("xxh3_128"), "yes"),
            ("ContentShare (blake3)", run_engine("blake3"), "yes"),
            ("ContentShare (sha256)", run_engine("sha256"), "yes"),
        ]
        rows = []
        for name, fn, verified in methods:
            times = time_method(name, fn, src, dest_root, args.repeat)
            best, median = min(times), statistics.median(times)
            rows.append((name, verified, best, total / MIB / best, median))

        base = next(r for r in rows if r[0] == "shutil.copy2")[2]
        lines = [
            f"Dataset: {files} files, {gib:.2f} GiB (one large file + {args.small_count} clips, "
            f"random data). Best of {args.repeat} runs. {sys.platform}, "
            f"Python {sys.version.split()[0]}.",
            "",
            "| Method | Verified | Best time | Throughput | vs shutil.copy2 |",
            "|---|---|---:|---:|---:|",
        ]
        lines += [
            f"| {n} | {v} | {t:.1f} s | {mb:,.0f} MB/s | {t / base:.2f}x |"
            for n, v, t, mb, _ in rows
        ]
        report = "\n".join(lines)
        print("\n" + report)
        if args.markdown:
            args.markdown.write_text(report + "\n", encoding="utf-8")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

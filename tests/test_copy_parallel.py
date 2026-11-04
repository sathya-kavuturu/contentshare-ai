"""Read-ahead and several files at once: faster, never a wrong file under its final name."""

from __future__ import annotations

import contextlib
import hashlib
import os
import threading
from pathlib import Path

import pytest

from contentshare.copy_engine import MIB, Control, CopyOptions, build_plan, copy_tree
from contentshare.copy_engine.engine import PARTIAL_SUFFIX
from contentshare.copy_engine.models import Hooks, Progress
from contentshare.copy_engine.readahead import read_ahead


def _no_sleep(_s: float) -> None:
    return None


def _options(parallel: int, **kw: object) -> CopyOptions:
    return CopyOptions(
        chunk_bytes=MIB, backoff_base_s=0, parallel_files=parallel, write_checksum_file=False, **kw
    )  # type: ignore[arg-type]


@pytest.fixture
def many(tmp_path: Path) -> Path:
    root = tmp_path / "src"
    for i in range(24):
        folder = root / f"part_{i % 4}"
        folder.mkdir(parents=True, exist_ok=True)
        # sizes around the chunk size, so the loop sees whole, partial and one-byte chunks
        (folder / f"clip_{i:02d}.mp4").write_bytes(os.urandom(MIB * (i % 3) + 1 + i * 997))
    (root / "big.mxf").write_bytes(os.urandom(5 * MIB + 17))
    (root / "empty.dat").write_bytes(b"")
    return root


def _same_tree(a: Path, b: Path) -> bool:
    names = sorted(p.relative_to(a) for p in a.rglob("*") if p.is_file())
    found = sorted(p.relative_to(b) for p in b.rglob("*") if p.is_file())
    return names == found and all((a / n).read_bytes() == (b / n).read_bytes() for n in names)


def _leftovers(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.name.endswith(PARTIAL_SUFFIX)]


# ------------------------------------------------------------------ read_ahead
def _reader(data: bytes, size: int):  # type: ignore[no-untyped-def]
    views = [memoryview(bytearray(size)) for _ in range(3)]
    position = {"at": 0}

    def read(i: int) -> int:
        piece = data[position["at"] : position["at"] + size]
        views[i][: len(piece)] = piece
        position["at"] += len(piece)
        return len(piece)

    return read, views


@pytest.mark.parametrize("length", [0, 1, 4095, 4096, 4097, 100_000])
def test_read_ahead_yields_every_byte_in_order(length: int) -> None:
    data = os.urandom(length)
    read, views = _reader(data, 4096)
    seen = hashlib.sha256()
    out = bytearray()
    for piece in read_ahead(read, views, seen.update):
        out += piece
    assert bytes(out) == data
    assert seen.digest() == hashlib.sha256(data).digest()


def test_read_ahead_hands_a_read_error_to_the_caller() -> None:
    views = [memoryview(bytearray(16)) for _ in range(3)]
    calls = {"n": 0}

    def read(_i: int) -> int:
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("network name is no longer available")
        return 16

    with pytest.raises(OSError, match="no longer available"):
        for _ in read_ahead(read, views):
            pass


def test_read_ahead_stops_its_thread_when_closed_early() -> None:
    read, views = _reader(os.urandom(1_000_000), 4096)
    before = threading.active_count()
    chunks = read_ahead(read, views)
    with contextlib.closing(chunks):
        next(chunks)
    assert threading.active_count() == before


def test_read_ahead_runs_ahead_but_never_more_than_the_buffers() -> None:
    in_flight = {"max": 0, "now": 0}
    views = [memoryview(bytearray(8)) for _ in range(3)]
    left = {"n": 40}

    def read(_i: int) -> int:
        if not left["n"]:
            return 0
        left["n"] -= 1
        in_flight["now"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["now"])
        return 8

    for _ in read_ahead(read, views):
        in_flight["now"] -= 1
    assert in_flight["max"] <= len(views)


# ------------------------------------------------------------------ several files at once
@pytest.mark.parametrize("parallel", [2, 4, 8])
def test_parallel_copy_is_exact_and_ordered(many: Path, tmp_path: Path, parallel: int) -> None:
    dst = tmp_path / "out"
    done: list[str] = []
    result = copy_tree(
        build_plan(many, dst),
        _options(parallel),
        sleep=_no_sleep,
        on_file_done=lambda r: done.append(r.rel),
    )
    assert result.ok
    assert _same_tree(many, dst)
    assert not _leftovers(dst)
    assert done == [f.rel for f in build_plan(many, tmp_path / "x").files]  # plan order


def test_parallel_and_serial_give_the_same_checksums(many: Path, tmp_path: Path) -> None:
    serial = copy_tree(build_plan(many, tmp_path / "a"), _options(1), sleep=_no_sleep)
    parallel = copy_tree(build_plan(many, tmp_path / "b"), _options(4), sleep=_no_sleep)
    assert {r.rel: r.hash_hex for r in serial.files} == {r.rel: r.hash_hex for r in parallel.files}


def test_parallel_progress_adds_up_and_reaches_100(many: Path, tmp_path: Path) -> None:
    events: list[Progress] = []
    result = copy_tree(
        build_plan(many, tmp_path / "out"),
        _options(4),
        sleep=_no_sleep,
        progress=events.append,
    )
    assert result.ok
    assert events[-1].fraction == pytest.approx(1.0)
    assert all(e.copied_bytes <= e.total_bytes for e in events)
    assert all(e.verified_bytes <= e.total_bytes for e in events)
    assert events[-1].copied_bytes == events[-1].total_bytes
    assert events[-1].verified_bytes == events[-1].total_bytes


def test_parallel_bit_flip_is_detected_and_only_that_file_is_recopied(
    many: Path, tmp_path: Path
) -> None:
    dst = tmp_path / "out"
    flipped = {"done": False}
    lock = threading.Lock()

    def corrupt_once(tmp: Path) -> None:
        with lock:
            if flipped["done"] or not tmp.name.startswith("big.mxf"):
                return
            flipped["done"] = True
        with tmp.open("r+b") as handle:
            handle.seek(3 * MIB)
            byte = handle.read(1)
            handle.seek(3 * MIB)
            handle.write(bytes([byte[0] ^ 0x01]))

    events: list[Progress] = []
    result = copy_tree(
        build_plan(many, dst),
        _options(4),
        sleep=_no_sleep,
        hooks=Hooks(after_write=corrupt_once),
        progress=events.append,
    )
    assert result.ok
    assert {r.rel: r.attempts for r in result.files}["big.mxf"] == 2
    assert all(r.attempts == 1 for r in result.files if r.rel != "big.mxf")
    assert _same_tree(many, dst)
    # the failed attempt was taken back: each byte is counted once, not twice
    assert events[-1].copied_bytes == events[-1].total_bytes
    assert events[-1].verified_bytes == events[-1].total_bytes


def test_parallel_persistent_corruption_fails_only_that_file(many: Path, tmp_path: Path) -> None:
    dst = tmp_path / "out"

    def always_corrupt(tmp: Path) -> None:
        if tmp.name.startswith("clip_05"):
            tmp.write_bytes(b"garbage")

    result = copy_tree(
        build_plan(many, dst),
        _options(4),
        sleep=_no_sleep,
        hooks=Hooks(after_write=always_corrupt),
    )
    assert not result.ok
    assert [r.rel for r in result.failures] == ["part_1/clip_05.mp4"]
    assert not (dst / "part_1" / "clip_05.mp4").exists()
    assert not _leftovers(dst)
    assert result.manifest_path is None
    others = [r for r in result.files if r.rel != "part_1/clip_05.mp4"]
    assert all((dst / r.rel).read_bytes() == (many / r.rel).read_bytes() for r in others)


def test_parallel_cancel_stops_everything_and_leaves_no_partials(
    many: Path, tmp_path: Path
) -> None:
    dst = tmp_path / "out"
    control = Control()
    started = {"n": 0}
    lock = threading.Lock()

    def cancel_after_a_few(_rel: str, _attempt: int) -> None:
        with lock:
            started["n"] += 1
            if started["n"] == 6:
                control.cancel()

    result = copy_tree(
        build_plan(many, dst),
        _options(4),
        control,
        sleep=_no_sleep,
        hooks=Hooks(on_attempt=cancel_after_a_few),
    )
    assert result.cancelled
    assert not result.ok
    assert not _leftovers(dst)
    assert result.manifest_path is None
    # whatever is in place is a complete, correct file
    for path in dst.rglob("*"):
        if path.is_file():
            assert path.read_bytes() == (many / path.relative_to(dst)).read_bytes()


def test_parallel_pause_blocks_every_worker_until_resumed(many: Path, tmp_path: Path) -> None:
    control = Control()
    control.pause()
    finished = threading.Event()
    outcome: list[bool] = []

    def worker() -> None:
        outcome.append(
            copy_tree(build_plan(many, tmp_path / "out"), _options(4), control, sleep=_no_sleep).ok
        )
        finished.set()

    thread = threading.Thread(target=worker)
    thread.start()
    assert not finished.wait(0.7)
    assert not (tmp_path / "out").exists() or not [
        p for p in (tmp_path / "out").rglob("*") if p.is_file()
    ]
    control.resume()
    assert finished.wait(30)
    thread.join()
    assert outcome == [True]


def test_parallel_conflicts_are_reported_and_nothing_is_overwritten(
    many: Path, tmp_path: Path
) -> None:
    dst = tmp_path / "out"
    (dst / "part_0").mkdir(parents=True)
    (dst / "part_0" / "clip_00.mp4").write_bytes(b"someone else's file")
    result = copy_tree(build_plan(many, dst), _options(4), sleep=_no_sleep)
    assert not result.ok
    assert [r.rel for r in result.failures] == ["part_0/clip_00.mp4"]
    assert (dst / "part_0" / "clip_00.mp4").read_bytes() == b"someone else's file"


def test_more_workers_than_files_is_fine(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "only.bin").write_bytes(os.urandom(100_000))
    result = copy_tree(build_plan(src, tmp_path / "out"), _options(8), sleep=_no_sleep)
    assert result.ok
    assert (tmp_path / "out" / "only.bin").read_bytes() == (src / "only.bin").read_bytes()

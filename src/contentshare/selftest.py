"""``contentshare --selftest``: proves an installed or freshly built program can do its job.

A packaged program can build fine and still fail at run time because a library, plug-in or data
file was left out. This exercises each one that matters, using only throwaway temporary files:
it never reads the user's settings or database, never contacts Gmail, never writes a secret.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import contentshare

Check = tuple[str, bool, str]


def _hashes() -> str:
    import blake3  # noqa: PLC0415
    import xxhash  # noqa: PLC0415

    data = b"contentshare"
    out = [xxhash.xxh3_128(data).hexdigest(), blake3.blake3(data).hexdigest()]
    out.append(hashlib.sha256(data).hexdigest())
    if len({len(x) for x in out}) != 2:
        raise RuntimeError("unexpected digest sizes")
    return "xxh3, blake3, sha256"


def _matching() -> str:
    from rapidfuzz import fuzz  # noqa: PLC0415

    score = fuzz.token_set_ratio("Sunrise Talk English", "2026-10-12_SunriseTalk_EN")
    if score < 40:
        raise RuntimeError(f"implausible score {score}")
    return "rapidfuzz"


def _database() -> str:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(name)")
        conn.execute("INSERT INTO t VALUES ('Sunrise Talk')")
        if not conn.execute("SELECT 1 FROM t WHERE t MATCH 'sunrise'").fetchone():
            raise RuntimeError("FTS5 search found nothing")
    finally:
        conn.close()
    return f"SQLite {sqlite3.sqlite_version} with FTS5"


def _config() -> str:
    from contentshare.config.models import AppConfig  # noqa: PLC0415

    config = AppConfig()
    if AppConfig.model_validate(config.model_dump()) != config:
        raise RuntimeError("settings did not round-trip")
    return "settings model"


def _keyring() -> str:
    import keyring  # noqa: PLC0415

    name = type(keyring.get_keyring()).__name__
    if "Win" not in name:
        raise RuntimeError(f"not the Windows Credential Manager backend: {name}")
    return name


def _google_client() -> str:
    import httplib2  # noqa: PLC0415
    from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: PLC0415
    from googleapiclient.discovery import build  # noqa: PLC0415

    # The Gmail API description ships inside the library; building from it must not need the
    # network (a packaged program that fetches it at start-up would fail offline).
    service = build("gmail", "v1", http=httplib2.Http(), static_discovery=True)
    if not hasattr(service.users().messages(), "send"):
        raise RuntimeError("Gmail API description is incomplete")
    return f"Gmail API description bundled ({InstalledAppFlow.__name__})"


def _copy_engine() -> str:
    from contentshare.copy_engine import (  # noqa: PLC0415
        CopyOptions,
        build_plan,
        copy_tree,
    )

    with tempfile.TemporaryDirectory(prefix="contentshare-selftest-") as tmp:
        src, dst = Path(tmp) / "src", Path(tmp) / "dst"
        (src / "sub").mkdir(parents=True)
        dst.mkdir()
        (src / "a.bin").write_bytes(os.urandom(300_000))
        (src / "sub" / "b.txt").write_text("hello", encoding="utf-8")
        result = copy_tree(build_plan(src, dst / "out"), CopyOptions(chunk_bytes=64 * 1024))
        if not result.ok or len(result.files) != 2:
            raise RuntimeError("verified copy failed")
        same = (src / "a.bin").read_bytes() == (dst / "out" / "a.bin").read_bytes()
        if not same:
            raise RuntimeError("copied bytes differ")
    return "copied and verified 2 files"


def _windows_start() -> str:
    from contentshare.app.startup import StartAtLogin  # noqa: PLC0415

    status = StartAtLogin().status()  # read-only
    return "start-up entry readable (" + ("on" if status.enabled else "off") + ")"


def _qt() -> str:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import PySide6  # noqa: PLC0415
    from PySide6.QtWidgets import QApplication, QLabel  # noqa: PLC0415

    from contentshare.ui.icons import app_icon  # noqa: PLC0415
    from contentshare.ui.theme import apply_theme  # noqa: PLC0415
    from contentshare.ui.tray import Tray  # noqa: PLC0415

    app = QApplication.instance() or QApplication([sys.argv[0]])
    apply_theme(app)  # type: ignore[arg-type]
    label = QLabel("ContentShare")
    label.resize(200, 40)
    image = label.grab().toImage()
    if image.isNull() or app_icon().isNull():
        raise RuntimeError("could not draw")
    Tray()  # the tray class and its menu build without a notification area
    return f"Qt {PySide6.__version__} draws widgets and icon"


CHECKS: list[tuple[str, Callable[[], str]]] = [
    ("hashing", _hashes),
    ("matching", _matching),
    ("database", _database),
    ("settings", _config),
    ("credential manager", _keyring),
    ("google client", _google_client),
    ("verified copy", _copy_engine),
    ("windows start-up", _windows_start),
    ("user interface", _qt),
]


def run_checks(progress: Callable[[str, list[Check]], None] | None = None) -> list[Check]:
    results: list[Check] = []
    for name, check in CHECKS:
        if progress:
            progress(name, results)  # if a check hangs, the report shows which one
        try:
            results.append((name, True, check()))
        except Exception as exc:
            results.append((name, False, f"{type(exc).__name__}: {exc}"))
    return results


def selftest(report_path: str | None = None) -> int:
    """Run every check. Writes a JSON report if a path is given (a windowed program has no
    console). Returns 0 only if everything passed."""

    def note(running: str, done: list[Check]) -> None:
        if report_path:
            partial = {"running": running, "done": [[n, p, d] for n, p, d in done]}
            Path(report_path).write_text(json.dumps(partial), encoding="utf-8")

    results = run_checks(note)
    ok = all(passed for _n, passed, _d in results)
    report = {
        "version": contentshare.__version__,
        "frozen": bool(getattr(sys, "frozen", False)),
        "python": sys.version.split()[0],
        "ok": ok,
        "checks": [{"name": n, "ok": p, "detail": d} for n, p, d in results],
    }
    text = json.dumps(report, indent=2)
    if report_path:
        Path(report_path).write_text(text, encoding="utf-8")
    elif sys.stdout is not None:
        print(text)
    return 0 if ok else 1

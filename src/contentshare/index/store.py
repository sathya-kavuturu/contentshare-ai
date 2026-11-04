"""A searchable index of the storage server: names, sizes and times, never file contents.

Searching the server directly is slow over a network, so we keep a local copy of the folder
and file names in SQLite (FTS5). Scanning only reads; nothing on the server is ever changed.
A scan that is cancelled or that cannot read the server never removes existing entries, so a
flaky network cannot empty the index.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from contentshare.config.models import IndexSettings
from contentshare.copy_engine.winio import extended, is_reparse_or_link
from contentshare.parsing.text import split_camel
from contentshare.storage.db import Database

log = logging.getLogger(__name__)

_FLUSH_EVERY = 1000
_PROGRESS_EVERY = 500
_MAX_QUERY_TOKENS = 8
_TOKEN = re.compile(r"[^\W_]+")

_SAFE_PHRASE = re.compile(r"[a-z0-9 ]{3,40}")  # what date_phrases() produces; nothing else


def _in_roots(column: str, roots: Sequence[str] | None) -> tuple[str, tuple[str, ...]]:
    """``AND <column> IN (?, ?)`` for a search limited to these storage folders, and the folders
    as parameters. ``None`` means every indexed folder. Only ``?`` marks go into the SQL."""
    if roots is None:
        return "", ()
    if not roots:
        return f" AND {column} IN (NULL)", ()  # no folders: nothing can match
    return f" AND {column} IN ({', '.join('?' for _ in roots)})", tuple(roots)


def _search_sql(roots: Sequence[str] | None) -> tuple[str, tuple[str, ...]]:
    clause, args = _in_roots("c.root", roots)
    return (
        "SELECT c.* FROM content_fts f JOIN content_items c ON c.id = f.rowid "  # noqa: S608
        f"WHERE content_fts MATCH ?{clause} ORDER BY bm25(content_fts, 10.0, 1.0) LIMIT ?",
        args,
    )


_UPSERT = (
    "INSERT INTO content_items (root, rel_path, name, path_text, kind, size, file_count, "
    "mtime_ns, depth, scan_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(root, rel_path) DO UPDATE SET kind = excluded.kind, size = excluded.size, "
    "file_count = excluded.file_count, mtime_ns = excluded.mtime_ns, depth = excluded.depth, "
    "scan_id = excluded.scan_id"
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class IndexedItem:
    id: int
    root: str
    rel_path: str  # "/" separated, relative to ``root``
    name: str
    kind: str  # "dir" | "file"
    size: int  # for a folder: total bytes inside
    file_count: int
    mtime_ns: int
    depth: int

    @property
    def is_dir(self) -> bool:
        return self.kind == "dir"

    @property
    def full_path(self) -> str:
        return self.root.rstrip("\\/") + "\\" + self.rel_path.replace("/", "\\")

    @property
    def parent_rel(self) -> str:
        return self.rel_path.rpartition("/")[0]


@dataclass
class ScanReport:
    root: str
    mode: str = "full"
    status: str = "complete"  # complete | cancelled | failed
    items: int = 0
    errors: int = 0
    seconds: float = 0.0
    error: str | None = None


class _Stop(Exception):  # noqa: N818 - control flow, not an error
    pass


def _row_to_item(row: Any) -> IndexedItem:
    return IndexedItem(
        id=row["id"], root=row["root"], rel_path=row["rel_path"], name=row["name"],
        kind=row["kind"], size=row["size"], file_count=row["file_count"],
        mtime_ns=row["mtime_ns"], depth=row["depth"],
    )  # fmt: skip


def query_tokens(text: str) -> list[str]:
    """Words worth searching for, lower-cased, camelCase split. Only letters and digits survive,
    so nothing from an email can change the meaning of the search query."""
    words = _TOKEN.findall(split_camel(text).casefold())
    seen: dict[str, None] = {}
    for word in words:
        if len(word) >= 2:
            seen.setdefault(word)
    return list(seen)[:_MAX_QUERY_TOKENS]


class ContentIndex:
    def __init__(self, db: Database, settings: IndexSettings | None = None) -> None:
        self.db = db
        self.settings = settings or IndexSettings()

    # ------------------------------------------------------------------ scanning
    def scan_root(
        self,
        root: str,
        *,
        progress: Callable[[int, str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> ScanReport:
        report = ScanReport(root)
        started = time.monotonic()
        with self.db.transaction() as conn:
            scan_id = int(
                conn.execute(
                    "INSERT INTO index_scans (root, mode, status, started_at) "
                    "VALUES (?, ?, 'running', ?)",
                    (root, report.mode, _now()),
                ).lastrowid
                or 0
            )
        scanner = _Scanner(self, root, scan_id, progress, should_stop)
        try:
            scanner.run()
            scanner.flush()
            self._drop_unseen(root, scan_id)
        except _Stop:
            scanner.flush()
            report.status = "cancelled"
        except OSError as exc:
            report.status, report.error = "failed", f"{type(exc).__name__}: {exc}"
        report.items, report.errors = scanner.items, scanner.errors
        report.seconds = time.monotonic() - started
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE index_scans SET status = ?, finished_at = ?, items = ?, errors = ? "
                "WHERE id = ?",
                (report.status, _now(), report.items, report.errors, scan_id),
            )
        return report

    def scan_all(self, roots: Iterable[str], **kwargs: Any) -> list[ScanReport]:
        reports = [self.scan_root(root, **kwargs) for root in roots]
        return reports

    def _drop_unseen(self, root: str, scan_id: int) -> None:
        """Only index rows are removed. Nothing on the storage server is touched."""
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM content_items WHERE root = ? AND scan_id < ?", (root, scan_id)
            )

    def forget_root(self, root: str) -> None:
        """A root removed from the settings: drop its index rows."""
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM content_items WHERE root = ?", (root,))

    # ------------------------------------------------------------------ searching
    def search(
        self,
        text: str,
        limit: int = 300,
        *,
        fuzzy: bool = True,
        date_phrases: Sequence[str] = (),
        roots: Sequence[str] | None = None,
    ) -> list[IndexedItem]:
        """Items whose folder/file name looks related to ``text``. ``roots`` limits the search to
        those storage folders (a department's own); ``None`` searches everything indexed. Cheap
        and broad: the matcher scores these properly afterwards. With ``fuzzy`` (the default),
        when few exact word hits exist, names that merely share a few letters are added to
        catch typos.

        ``date_phrases`` (see ``parsing.dates.date_phrases``) make items whose name or path
        contains one of the requested dates come first. Without this, a program that recurs
        hundreds of times in the archive would fill the result list with arbitrary dates."""
        tokens = query_tokens(text)
        if not tokens:
            return []
        fts = " OR ".join(f'"{t}"*' for t in tokens)  # tokens are [letters digits] only
        found: dict[int, IndexedItem] = {}
        phrases = [p for p in date_phrases if _SAFE_PHRASE.fullmatch(p)]
        sql, root_args = _search_sql(roots)

        def run(conn: Any, match: str) -> list[IndexedItem]:
            return [_row_to_item(r) for r in conn.execute(sql, (match, *root_args, limit))]

        with self.db.read() as conn:
            if phrases:
                dates = " OR ".join(f'"{p}"' for p in phrases)
                scoped = f"{{name path_text}} : ({fts}) AND {{name path_text}} : ({dates})"
                found.update({i.id: i for i in run(conn, scoped)})
            dated = len(found)
            for column_filter in ("{name}", "{name path_text}"):
                found.update({i.id: i for i in run(conn, f"{column_filter} : ({fts})")})
                if len(found) - dated >= 30:  # name hits are enough; skip the broader path search
                    break
            if fuzzy and len(found) < 5:  # typo-tolerant fallback: share a few letters with a word
                for item in self._fuzzy_fallback(conn, tokens, limit, roots):
                    found.setdefault(item.id, item)
        return list(found.values())[:limit]

    @staticmethod
    def _fuzzy_fallback(
        conn: Any, tokens: list[str], limit: int, roots: Sequence[str] | None = None
    ) -> list[IndexedItem]:
        clauses, args = [], []
        for token in tokens:
            if len(token) >= 4:
                for piece in {token[:3], token[-3:]}:
                    clauses.append("name LIKE ?")
                    args.append(f"%{piece}%")
        if not clauses:
            return []
        scope, root_args = _in_roots("root", roots)
        rows = conn.execute(
            # Only the constant "name LIKE ?" and "?" marks are joined into the SQL; every value is
            # a bound parameter, so nothing from a request can alter the query.
            f"SELECT * FROM content_items WHERE ({' OR '.join(clauses)}){scope} "  # noqa: S608  # nosec B608
            "ORDER BY depth LIMIT ?",
            (*args, *root_args, min(limit * 3, 1000)),
        ).fetchall()
        return [_row_to_item(r) for r in rows]

    def find_dirs_named(
        self, names: Sequence[str], roots: Sequence[str] | None = None
    ) -> dict[str, list[IndexedItem]]:
        """Folders whose name is exactly one of ``names`` (capitals do not matter), by the name
        asked for, in ``roots`` (``None``: everywhere indexed). Names with no folder are left out.
        Every value is a bound parameter."""
        found: dict[str, list[IndexedItem]] = {}
        scope, root_args = _in_roots("root", roots)
        with self.db.read() as conn:
            for name in names:
                rows = conn.execute(
                    "SELECT * FROM content_items WHERE name = ? COLLATE NOCASE AND kind = 'dir'"  # noqa: S608
                    f"{scope} ORDER BY depth, root, rel_path LIMIT 50",
                    (name, *root_args),
                ).fetchall()
                if rows:
                    found[name] = [_row_to_item(r) for r in rows]
        return found

    def get(self, item_id: int) -> IndexedItem | None:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM content_items WHERE id = ?", (item_id,)).fetchone()
        return _row_to_item(row) if row else None

    def find_path(self, root: str, rel_path: str) -> IndexedItem | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM content_items WHERE root = ? AND rel_path = ?", (root, rel_path)
            ).fetchone()
        return _row_to_item(row) if row else None

    def last_scan(self, root: str) -> dict[str, Any] | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM index_scans WHERE root = ? AND status = 'complete' "
                "ORDER BY id DESC LIMIT 1",
                (root,),
            ).fetchone()
        return dict(row) if row else None

    def stats(self) -> dict[str, int]:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS items, "
                "COALESCE(SUM(kind = 'dir'), 0) AS folders FROM content_items"
            ).fetchone()
        return {"items": row["items"], "folders": row["folders"]}


class _Scanner:
    def __init__(
        self,
        index: ContentIndex,
        root: str,
        scan_id: int,
        progress: Callable[[int, str], None] | None,
        should_stop: Callable[[], bool] | None,
    ) -> None:
        self.index, self.root, self.scan_id = index, root, scan_id
        self.progress, self.should_stop = progress, should_stop
        self.excludes = [p.casefold() for p in index.settings.exclude_names]
        self.index_files = index.settings.index_files
        self.rows: list[tuple[Any, ...]] = []
        self.items = 0
        self.errors = 0
        self.read: Any = None  # read connection, open for the duration of run()

    def run(self) -> None:
        root_path = Path(self.root)
        if not root_path.is_dir():
            raise FileNotFoundError(f"Cannot reach {self.root}")
        with self.index.db.read() as conn:
            self.read = conn
            self._scan_dir(root_path, "", 0, 0)

    # -- helpers
    def _excluded(self, name: str) -> bool:
        folded = name.casefold()
        return any(fnmatch.fnmatchcase(folded, pattern) for pattern in self.excludes)

    def _stored(self, rel: str) -> Any:
        return self.read.execute(
            "SELECT kind, size, file_count, mtime_ns FROM content_items "
            "WHERE root = ? AND rel_path = ?",
            (self.root, rel),
        ).fetchone()

    def _keep_subtree(self, rel: str) -> None:
        """Mark a folder and everything below it as still present without re-reading it."""
        with self.index.db.transaction() as conn:
            conn.execute(
                "UPDATE content_items SET scan_id = ? WHERE root = ? AND "
                "(rel_path = ? OR (rel_path >= ? AND rel_path < ?))",
                (self.scan_id, self.root, rel, rel + "/", rel + "0"),  # "/" and "0" are adjacent
            )

    def _add(
        self, rel: str, name: str, kind: str, size: int, count: int, mtime: int, depth: int
    ) -> None:
        path_text = rel.replace("/", " ")
        self.rows.append(
            (self.root, rel, name, path_text, kind, size, count, mtime, depth, self.scan_id)
        )
        self.items += 1
        if len(self.rows) >= _FLUSH_EVERY:
            self.flush()
        if self.progress and self.items % _PROGRESS_EVERY == 0:
            self.progress(self.items, rel)

    def flush(self) -> None:
        if not self.rows:
            return
        with self.index.db.transaction() as conn:
            conn.executemany(_UPSERT, self.rows)
        self.rows = []

    # -- the walk
    def _scan_dir(self, path: Path, rel: str, depth: int, mtime: int) -> tuple[int, int]:
        try:
            with os.scandir(extended(path)) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            if not rel:
                raise
            self.errors += 1
            log.warning("cannot read %s: %s", path, exc)
            self._keep_subtree(rel)  # unreadable now does not mean gone
            stored = self._stored(rel)
            return (stored["size"], stored["file_count"]) if stored else (0, 0)

        size = count = 0
        for entry in entries:
            if self.should_stop and self.should_stop():
                raise _Stop
            if self._excluded(entry.name) or is_reparse_or_link(entry):
                continue
            child = f"{rel}/{entry.name}" if rel else entry.name
            try:
                if entry.is_dir(follow_symlinks=False):
                    s, c = self._child_dir(path / entry.name, child, entry, depth + 1)
                    size, count = size + s, count + c
                elif entry.is_file(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    size, count = size + st.st_size, count + 1
                    if self.index_files:
                        self._add(
                            child, entry.name, "file", st.st_size, 1, st.st_mtime_ns, depth + 1
                        )
            except OSError as exc:
                self.errors += 1
                log.warning("cannot read %s: %s", child, exc)
        if rel:
            self._add(rel, path.name, "dir", size, count, mtime, depth)
        return size, count

    def _child_dir(
        self, path: Path, rel: str, entry: os.DirEntry[str], depth: int
    ) -> tuple[int, int]:
        return self._scan_dir(path, rel, depth, entry.stat(follow_symlinks=False).st_mtime_ns)


def measure(path: Path) -> tuple[int, int] | None:
    """Size and file count of a folder or file as it is on disk right now, or ``None`` if it
    is gone. Used to re-check a match just before copying; links are not followed."""
    if path.is_file():
        return path.stat().st_size, 1
    if not path.is_dir():
        return None
    size = count = 0
    stack = [path]
    while stack:
        with os.scandir(extended(stack.pop())) as it:
            for entry in it:
                if is_reparse_or_link(entry):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    size += entry.stat(follow_symlinks=False).st_size
                    count += 1
    return size, count

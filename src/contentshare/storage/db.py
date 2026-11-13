"""SQLite connection handling and schema migrations."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS: list[str] = [
    # --- v1 ---
    """
    CREATE TABLE emails (
        id               INTEGER PRIMARY KEY,
        gmail_message_id TEXT NOT NULL UNIQUE,
        gmail_thread_id  TEXT NOT NULL,
        sender_email     TEXT NOT NULL,
        sender_name      TEXT NOT NULL DEFAULT '',
        department       TEXT,
        subject          TEXT NOT NULL DEFAULT '',
        snippet          TEXT NOT NULL DEFAULT '',
        received_at      TEXT NOT NULL,
        auth_status      TEXT NOT NULL CHECK (auth_status IN ('verified', 'suspicious', 'unknown')),
        ignored_reason   TEXT,
        created_at       TEXT NOT NULL
    );

    CREATE TABLE requests (
        id               INTEGER PRIMARY KEY,
        email_id         INTEGER NOT NULL REFERENCES emails(id),
        line_no          INTEGER NOT NULL,
        query_json       TEXT NOT NULL DEFAULT '{}',
        source_path      TEXT,
        confidence       REAL,
        file_count       INTEGER,
        total_bytes      INTEGER,
        destination_path TEXT,
        auto_reply       INTEGER NOT NULL DEFAULT 0,
        state            TEXT NOT NULL,
        failure_reason   TEXT,
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL,
        UNIQUE (email_id, line_no)
    );
    CREATE INDEX idx_requests_state ON requests(state);

    CREATE TABLE jobs (
        id          INTEGER PRIMARY KEY,
        request_id  INTEGER NOT NULL REFERENCES requests(id),
        state       TEXT NOT NULL,
        attempt     INTEGER NOT NULL DEFAULT 1,
        started_at  TEXT,
        finished_at TEXT,
        error       TEXT
    );

    CREATE TABLE job_files (
        id          INTEGER PRIMARY KEY,
        job_id      INTEGER NOT NULL REFERENCES jobs(id),
        rel_path    TEXT NOT NULL,
        size        INTEGER NOT NULL,
        hash_algo   TEXT NOT NULL,
        source_hash TEXT,
        dest_hash   TEXT,
        state       TEXT NOT NULL,
        attempts    INTEGER NOT NULL DEFAULT 0,
        error       TEXT,
        UNIQUE (job_id, rel_path)
    );

    CREATE TABLE replies (
        id               INTEGER PRIMARY KEY,
        request_id       INTEGER NOT NULL REFERENCES requests(id),
        gmail_message_id TEXT,
        body             TEXT NOT NULL,
        status           TEXT NOT NULL,
        sent_at          TEXT
    );

    CREATE TABLE audit_log (
        id         INTEGER PRIMARY KEY,
        ts         TEXT NOT NULL,
        actor      TEXT NOT NULL,
        event      TEXT NOT NULL,
        request_id INTEGER,
        detail     TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
    BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
    CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
    BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
    """,
    # --- v2: a restarted job can skip files it already verified (size + mtime still match) ---
    """
    ALTER TABLE job_files ADD COLUMN mtime_ns INTEGER;
    """,
    # --- v3: searchable index of the storage server (names, sizes, times; never file contents) ---
    """
    CREATE TABLE index_scans (
        id          INTEGER PRIMARY KEY,
        root        TEXT NOT NULL,
        mode        TEXT NOT NULL,
        status      TEXT NOT NULL,
        started_at  TEXT NOT NULL,
        finished_at TEXT,
        items       INTEGER NOT NULL DEFAULT 0,
        errors      INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE content_items (
        id         INTEGER PRIMARY KEY,
        root       TEXT NOT NULL,
        rel_path   TEXT NOT NULL,
        name       TEXT NOT NULL,
        path_text  TEXT NOT NULL,
        kind       TEXT NOT NULL CHECK (kind IN ('dir', 'file')),
        size       INTEGER NOT NULL DEFAULT 0,
        file_count INTEGER NOT NULL DEFAULT 0,
        mtime_ns   INTEGER NOT NULL,
        depth      INTEGER NOT NULL,
        scan_id    INTEGER NOT NULL,
        UNIQUE (root, rel_path)
    );
    CREATE INDEX idx_content_scan ON content_items(root, scan_id);

    CREATE VIRTUAL TABLE content_fts USING fts5(
        name, path_text,
        content = 'content_items', content_rowid = 'id',
        tokenize = 'unicode61 remove_diacritics 2'
    );
    CREATE TRIGGER content_ai AFTER INSERT ON content_items
    BEGIN
        INSERT INTO content_fts(rowid, name, path_text) VALUES (new.id, new.name, new.path_text);
    END;
    CREATE TRIGGER content_ad AFTER DELETE ON content_items
    BEGIN
        INSERT INTO content_fts(content_fts, rowid, name, path_text)
        VALUES ('delete', old.id, old.name, old.path_text);
    END;
    CREATE TRIGGER content_au AFTER UPDATE OF name, path_text ON content_items
    BEGIN
        INSERT INTO content_fts(content_fts, rowid, name, path_text)
        VALUES ('delete', old.id, old.name, old.path_text);
        INSERT INTO content_fts(rowid, name, path_text) VALUES (new.id, new.name, new.path_text);
    END;
    """,
    # --- v4: what a threaded reply needs, and a journal for crash-safe sending ---
    """
    ALTER TABLE emails ADD COLUMN rfc_message_id TEXT;
    ALTER TABLE emails ADD COLUMN references_header TEXT;
    ALTER TABLE replies ADD COLUMN purpose TEXT NOT NULL DEFAULT 'shared';
    ALTER TABLE replies ADD COLUMN token TEXT;
    ALTER TABLE replies ADD COLUMN to_addr TEXT;
    ALTER TABLE replies ADD COLUMN error TEXT;
    ALTER TABLE replies ADD COLUMN created_at TEXT;
    CREATE UNIQUE INDEX idx_replies_one_active ON replies(request_id, purpose)
        WHERE status IN ('SENDING', 'SENT');
    """,
    # --- v5: look up a folder by its exact name (capitals do not matter, as in Windows) ---
    """
    CREATE INDEX idx_content_name ON content_items(name COLLATE NOCASE, kind);
    """,
    # --- v6: the people who use the app (a name and a salted password hash each) ---
    """
    CREATE TABLE users (
        id         INTEGER PRIMARY KEY,
        name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
        salt       TEXT NOT NULL,
        pw_hash    TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_login TEXT
    );
    """,
    # --- v7: what the person decided about emails the smart reader read (shown as examples) ---
    """
    CREATE TABLE smart_examples (
        id           INTEGER PRIMARY KEY,
        email_id     INTEGER NOT NULL UNIQUE,
        text         TEXT NOT NULL,
        is_request   INTEGER NOT NULL,
        folders_json TEXT NOT NULL DEFAULT '[]',
        created_at   TEXT NOT NULL
    );
    """,
    # --- v8: every mail that was looked at keeps its Gmail labels and a short text (for the
    #         Mailbox view); where the last check got to; senders always skipped or accepted ---
    """
    ALTER TABLE emails ADD COLUMN labels TEXT NOT NULL DEFAULT '';
    CREATE TABLE scan_state (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE sender_rules (
        sender     TEXT PRIMARY KEY,
        action     TEXT NOT NULL CHECK (action IN ('skip', 'request')),
        created_at TEXT NOT NULL
    );
    """,
    # --- v9: a request is a share (archive to a department) or a backup (a department's own
    #         content into the archive) ---
    """
    ALTER TABLE requests ADD COLUMN kind TEXT NOT NULL DEFAULT 'share';
    """,
]


class Database:
    """Opens a fresh connection per unit of work; SQLite handles this cheaply and it keeps
    worker threads (copy queue, Gmail, UI) from sharing one connection."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")  # a crash must not lose the journal
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def migrate(self) -> int:
        conn = self._connect()
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > len(MIGRATIONS):
                raise RuntimeError(
                    f"Database is version {version} but this app only knows {len(MIGRATIONS)}. "
                    "Update ContentShare."
                )
            for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in _split_script(script):
                        conn.execute(statement)
                    conn.execute(f"PRAGMA user_version = {number}")  # int, not user input
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()


def _split_script(script: str) -> list[str]:
    """Split on ';' but keep CREATE TRIGGER ... BEGIN ... END; bodies whole."""
    statements: list[str] = []
    buffer: list[str] = []
    in_trigger = False
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        buffer.append(line)
        upper = stripped.upper()
        if upper.startswith("CREATE TRIGGER"):
            in_trigger = True
        if in_trigger:
            if upper.endswith("END;"):
                in_trigger = False
                statements.append("\n".join(buffer))
                buffer = []
        elif stripped.endswith(";"):
            statements.append("\n".join(buffer))
            buffer = []
    if buffer:
        statements.append("\n".join(buffer))
    return statements

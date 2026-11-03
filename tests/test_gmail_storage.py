from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from contentshare.config.models import AppConfig, GmailSettings
from contentshare.storage.db import MIGRATIONS, Database, _split_script
from contentshare.storage.repository import Repository


def test_gmail_settings_defaults_are_the_safe_ones() -> None:
    g = AppConfig().gmail
    assert g.require_authentication is True
    assert g.trusted_authserv_ids == ["mx.google.com"]
    assert (g.lookback_days, g.max_messages, g.apply_label) == (30, 100, True)
    assert g.client_secret_path is None


@pytest.mark.parametrize(
    "bad",
    [
        {"lookback_days": 0},
        {"lookback_days": 1000},
        {"max_messages": 0},
        {"max_messages": 5000},
        {"processed_label": ""},
        {"processed_label": "x" * 101},
        {"processed_label": "bad\nlabel"},
        {"unknown_option": True},
    ],
)
def test_gmail_settings_are_validated(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        GmailSettings(**bad)  # type: ignore[arg-type]


def test_trusted_servers_are_normalised() -> None:
    assert GmailSettings(trusted_authserv_ids=[" MX.Google.com ", ""]).trusted_authserv_ids == [
        "mx.google.com"
    ]


def test_upgrade_from_v3_adds_reply_columns_and_the_double_send_guard(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    for script in MIGRATIONS[:3]:
        for statement in _split_script(script):
            conn.execute(statement)
    conn.execute("PRAGMA user_version = 3")
    conn.execute(
        "INSERT INTO emails (gmail_message_id, gmail_thread_id, sender_email, received_at, "
        "auth_status, created_at) VALUES ('old', 't', 'a@b.org', 'x', 'verified', 'x')"
    )
    conn.commit()
    conn.close()

    db = Database(path)
    with db.read() as c:
        email_columns = {r["name"] for r in c.execute("PRAGMA table_info(emails)")}
        reply_columns = {r["name"] for r in c.execute("PRAGMA table_info(replies)")}
        assert c.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 1
    assert {"rfc_message_id", "references_header"} <= email_columns
    assert {"purpose", "token", "to_addr", "error", "created_at"} <= reply_columns


def test_database_allows_one_active_reply_per_request_but_failed_ones_do_not_count(
    repo: Repository,
) -> None:
    email_id = repo.add_email(
        gmail_message_id="m", gmail_thread_id="t", sender_email="a@b.org",
        received_at="x", auth_status="verified",
    )  # fmt: skip
    assert email_id is not None
    request_id = repo.add_request(email_id, 1, {})
    first = repo.begin_reply(request_id, purpose="shared", to_addr="a@b.org", body="x", token="1")
    with pytest.raises(sqlite3.IntegrityError):
        repo.begin_reply(request_id, purpose="shared", to_addr="a@b.org", body="x", token="2")
    repo.begin_reply(
        request_id, purpose="rejection", to_addr="a@b.org", body="x", token="3"
    )  # other kind
    repo.finish_reply(first, sent=False, error="refused")
    repo.begin_reply(
        request_id, purpose="shared", to_addr="a@b.org", body="x", token="4"
    )  # retry ok


def test_thread_known_and_unknown_reply(repo: Repository) -> None:
    assert not repo.thread_known("T")
    repo.add_email(
        gmail_message_id="m", gmail_thread_id="T", sender_email="a@b.org",
        received_at="x", auth_status="verified",
    )  # fmt: skip
    assert repo.thread_known("T")
    with pytest.raises(KeyError):
        repo.finish_reply(999, sent=True)
    assert repo.get_reply(999) is None


def test_cli_gmail_commands_without_a_connection(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import keyring
    from keyring.backend import KeyringBackend

    from contentshare.cli import main

    class Empty(KeyringBackend):
        priority = 1

        def get_password(self, service: str, username: str) -> str | None:
            return None

        def set_password(self, service: str, username: str, password: str) -> None:
            pass

        def delete_password(self, service: str, username: str) -> None:
            pass

    previous = keyring.get_keyring()
    keyring.set_keyring(Empty())
    monkeypatch.setenv("CONTENTSHARE_HOME", str(tmp_path / "home"))
    try:
        assert main(["gmail-status"]) == 0
        assert "Not connected" in capsys.readouterr().out
        assert main(["gmail-connect"]) == 1  # no client file configured yet
        assert "client_secret_path" in capsys.readouterr().err
        assert main(["gmail-peek"]) == 1  # cannot peek without signing in
        assert "AuthRequiredError" in capsys.readouterr().err
        assert main(["gmail-disconnect"]) == 0
        assert "deleted" in capsys.readouterr().out
    finally:
        keyring.set_keyring(previous)

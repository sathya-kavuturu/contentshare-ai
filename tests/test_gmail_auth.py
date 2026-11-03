from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import keyring
import pytest
from google.auth.exceptions import RefreshError, TransportError
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from contentshare.config.models import GmailSettings
from contentshare.gmail.auth import (
    SCOPE_MODIFY,
    SCOPE_READ,
    SCOPE_SEND,
    GmailAuth,
    KeyringTokenStore,
    required_scopes,
)
from contentshare.gmail.errors import AuthRequiredError, TransientGmailError


class MemoryKeyring(KeyringBackend):
    """A throwaway keyring so tests never touch the real Windows Credential Manager."""

    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.data: dict[tuple[str, str], str] = {}
        self.max_entry = 0

    def set_password(self, service: str, username: str, password: str) -> None:
        self.max_entry = max(self.max_entry, len(password))
        self.data[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        return self.data.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.data:
            raise PasswordDeleteError("missing")
        del self.data[(service, username)]


@pytest.fixture
def vault() -> MemoryKeyring:
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend  # type: ignore[misc]
    keyring.set_keyring(previous)


SCOPES = [SCOPE_MODIFY, SCOPE_SEND]
SECRET = "1//refresh-token-SECRET-VALUE"


def token_data(**over: Any) -> dict[str, Any]:
    data = {
        "token": "ya29.access-token-SECRET",
        "refresh_token": SECRET,
        "expiry": (datetime.now() + timedelta(hours=1)).isoformat(),
        "scopes": sorted(SCOPES),
        "client_id": "cid.apps.googleusercontent.com",
        "client_secret": "client-SECRET",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    data.update(over)
    return data


class FakeCreds:
    refresh_error: Exception | None = None
    last: FakeCreds | None = None

    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)
        self.refreshed = False
        FakeCreds.last = self

    @property
    def valid(self) -> bool:
        return bool(self.token) and self.expiry is not None and self.expiry > datetime.now()

    def refresh(self, request: Any) -> None:
        if FakeCreds.refresh_error:
            raise FakeCreds.refresh_error
        self.refreshed = True
        self.token = "ya29.NEW-ACCESS"
        self.expiry = datetime.now() + timedelta(hours=1)


@pytest.fixture(autouse=True)
def _reset_fake_creds() -> None:
    FakeCreds.refresh_error = None
    FakeCreds.last = None


def make_auth(store: KeyringTokenStore, **kw: Any) -> GmailAuth:
    return GmailAuth(store, credentials_factory=FakeCreds, request_factory=object, **kw)


# ------------------------------------------------------------------ scopes
def test_scopes_are_the_narrowest_that_work() -> None:
    assert required_scopes(GmailSettings(apply_label=False)) == [SCOPE_READ, SCOPE_SEND]
    assert required_scopes(GmailSettings(apply_label=True)) == [SCOPE_MODIFY, SCOPE_SEND]
    for scopes in (
        required_scopes(GmailSettings()),
        required_scopes(GmailSettings(apply_label=False)),
    ):
        assert not any("mail.google.com" in s or s.endswith("/gmail.compose") for s in scopes)


# ------------------------------------------------------------------ token storage
def test_token_roundtrip_in_chunks_below_the_vault_limit(vault: MemoryKeyring) -> None:
    store = KeyringTokenStore()
    big = token_data(refresh_token="r" * 4000)
    store.save(big)
    assert store.load() == big
    assert vault.max_entry <= 1000  # Windows Credential Manager rejects entries over ~2.5 KB
    assert len(vault.data) > 3


def test_save_replaces_and_clear_removes_everything(vault: MemoryKeyring) -> None:
    store = KeyringTokenStore()
    store.save(token_data(refresh_token="r" * 3000))
    short = token_data()
    store.save(short)  # shorter: leftover chunks from before must not linger
    assert store.load() == short
    store.clear()
    assert store.load() is None
    assert vault.data == {}
    store.clear()  # clearing twice is fine


def test_damaged_vault_entries_read_as_not_connected(vault: MemoryKeyring) -> None:
    store = KeyringTokenStore()
    store.save(token_data())
    vault.data[("ContentShare", "gmail-oauth.0")] = "{not json"
    assert store.load() is None
    vault.data.pop(("ContentShare", "gmail-oauth.0"))
    assert store.load() is None
    vault.data[("ContentShare", "gmail-oauth.count")] = "abc"
    assert store.load() is None


# ------------------------------------------------------------------ connecting
def test_connect_stores_the_token_only_in_the_vault(vault: MemoryKeyring, tmp_path: Path) -> None:
    secret_file = tmp_path / "client_secret.json"
    secret_file.write_text("{}")
    auth = make_auth(KeyringTokenStore(), flow_runner=lambda path, scopes: token_data())
    auth.connect(str(secret_file), SCOPES)
    assert auth.is_connected()
    assert not list(tmp_path.glob("*token*"))  # nothing written to disk
    assert not any(p.suffix == ".json" and "token" in p.name for p in Path.cwd().glob("*.json"))


def test_connect_requires_the_client_file(vault: MemoryKeyring, tmp_path: Path) -> None:
    auth = make_auth(KeyringTokenStore(), flow_runner=lambda *_: pytest.fail("must not start"))
    with pytest.raises(AuthRequiredError, match="client file"):
        auth.connect(str(tmp_path / "missing.json"), SCOPES)


def test_connect_without_refresh_token_or_with_fewer_permissions_is_refused(
    vault: MemoryKeyring, tmp_path: Path
) -> None:
    f = tmp_path / "c.json"
    f.write_text("{}")
    no_refresh = make_auth(
        KeyringTokenStore(), flow_runner=lambda *_: token_data(refresh_token=None)
    )
    with pytest.raises(AuthRequiredError, match="long-term"):
        no_refresh.connect(str(f), SCOPES)
    fewer = make_auth(KeyringTokenStore(), flow_runner=lambda *_: token_data(scopes=[SCOPE_SEND]))
    with pytest.raises(AuthRequiredError, match="permissions"):
        fewer.connect(str(f), SCOPES)
    assert not KeyringTokenStore().load()  # a half-granted sign-in is not kept


def test_cancelled_sign_in_is_reported_without_details(
    vault: MemoryKeyring, tmp_path: Path
) -> None:
    f = tmp_path / "c.json"
    f.write_text("{}")

    def cancelled(*_: Any) -> dict[str, Any]:
        raise RuntimeError(f"user closed the window; code={SECRET}")

    with pytest.raises(AuthRequiredError) as info:
        make_auth(KeyringTokenStore(), flow_runner=cancelled).connect(str(f), SCOPES)
    assert SECRET not in str(info.value)


# ------------------------------------------------------------------ using the token
def test_valid_token_is_used_without_refreshing(vault: MemoryKeyring) -> None:
    store = KeyringTokenStore()
    store.save(token_data())
    creds = make_auth(store).credentials(SCOPES)
    assert creds.token.startswith("ya29.access")
    assert not creds.refreshed


def test_expired_token_is_refreshed_and_saved(vault: MemoryKeyring) -> None:
    store = KeyringTokenStore()
    store.save(token_data(expiry=(datetime.now() - timedelta(minutes=5)).isoformat()))
    creds = make_auth(store).credentials(SCOPES)
    assert creds.refreshed
    saved = store.load()
    assert saved is not None
    assert saved["token"] == "ya29.NEW-ACCESS"
    assert saved["refresh_token"] == SECRET  # the long-term token is kept


def test_not_connected_and_changed_permissions_ask_to_sign_in_again(vault: MemoryKeyring) -> None:
    store = KeyringTokenStore()
    auth = make_auth(store)
    with pytest.raises(AuthRequiredError, match="not connected"):
        auth.credentials(SCOPES)
    store.save(token_data(scopes=[SCOPE_READ, SCOPE_SEND]))  # connected earlier with labels off
    with pytest.raises(AuthRequiredError, match="permissions changed"):
        auth.credentials(SCOPES)
    assert auth.credentials([SCOPE_READ, SCOPE_SEND]) is not None


def test_revoked_access_and_network_failure_during_refresh(vault: MemoryKeyring) -> None:
    store = KeyringTokenStore()
    store.save(token_data(expiry=(datetime.now() - timedelta(hours=1)).isoformat()))
    auth = make_auth(store)
    FakeCreds.refresh_error = RefreshError("invalid_grant")
    with pytest.raises(AuthRequiredError, match="revoked"):
        auth.credentials(SCOPES)
    FakeCreds.refresh_error = TransportError("offline")
    with pytest.raises(TransientGmailError):
        auth.credentials(SCOPES)


# ------------------------------------------------------------------ disconnecting
def test_disconnect_revokes_then_forgets(vault: MemoryKeyring) -> None:
    store = KeyringTokenStore()
    store.save(token_data())
    calls: list[tuple[str, dict[str, str]]] = []
    auth = make_auth(store, http_post=lambda url, data, timeout: calls.append((url, data)) or 200)
    outcome = auth.disconnect()
    assert outcome.revoked
    assert calls == [("https://oauth2.googleapis.com/revoke", {"token": SECRET})]
    assert not auth.is_connected()
    assert vault.data == {}


def test_disconnect_forgets_the_token_even_if_google_is_unreachable(vault: MemoryKeyring) -> None:
    store = KeyringTokenStore()
    store.save(token_data())

    def offline(*_: Any) -> int:
        raise ConnectionError("offline")

    outcome = make_auth(store, http_post=offline).disconnect()
    assert not outcome.revoked
    assert "Could not reach Google" in (outcome.error or "")
    assert store.load() is None  # the local copy is gone regardless

    store.save(token_data())
    refused = make_auth(store, http_post=lambda *_: 400).disconnect()
    assert not refused.revoked
    assert "400" in (refused.error or "")


def test_disconnect_when_not_connected(vault: MemoryKeyring) -> None:
    outcome = make_auth(
        KeyringTokenStore(), http_post=lambda *_: pytest.fail("no token")
    ).disconnect()
    assert not outcome.revoked


def test_tokens_never_reach_the_log(
    vault: MemoryKeyring, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    caplog.set_level(logging.DEBUG)
    f = tmp_path / "c.json"
    f.write_text("{}")
    store = KeyringTokenStore()
    auth = make_auth(store, flow_runner=lambda *_: token_data(), http_post=lambda *_: 200)
    auth.connect(str(f), SCOPES)
    auth.credentials(SCOPES)
    auth.disconnect()
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert SECRET not in logged
    assert "ya29" not in logged
    assert "client-SECRET" not in logged
    assert json.dumps(token_data())[:20] not in logged

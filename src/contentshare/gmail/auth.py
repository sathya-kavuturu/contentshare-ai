"""Signing in to Gmail (OAuth 2.0 installed-app flow) and keeping the token safe.

* Your Google password is never seen, asked for or stored.
* The token lives only in Windows Credential Manager (via ``keyring``), never in a file.
* Permissions are the smallest set that works (see :func:`required_scopes`). If they change,
  the saved token no longer counts and you are asked to connect again.
* ``disconnect`` revokes the token with Google and deletes the stored copy.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import keyring
import requests
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from keyring.errors import PasswordDeleteError

from contentshare.config.models import GmailSettings
from contentshare.gmail.errors import AuthRequiredError, TransientGmailError

log = logging.getLogger(__name__)

SCOPE_READ = "https://www.googleapis.com/auth/gmail.readonly"
SCOPE_SEND = "https://www.googleapis.com/auth/gmail.send"
SCOPE_MODIFY = "https://www.googleapis.com/auth/gmail.modify"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
_CHUNK = 1000  # Windows Credential Manager limits one entry to about 2.5 KB


def required_scopes(settings: GmailSettings) -> list[str]:
    """Read + send, or modify + send when labelling processed mail is turned on. ``modify``
    includes reading; it is requested only because applying a label needs it."""
    return [SCOPE_MODIFY, SCOPE_SEND] if settings.apply_label else [SCOPE_READ, SCOPE_SEND]


class TokenStore(Protocol):
    def load(self) -> dict[str, Any] | None: ...
    def save(self, data: dict[str, Any]) -> None: ...
    def clear(self) -> None: ...


class KeyringTokenStore:
    """Token in Windows Credential Manager, split into chunks to stay under its size limit."""

    def __init__(self, service: str = "ContentShareAI", name: str = "gmail-oauth") -> None:
        self.service, self.name = service, name

    def _get(self, key: str) -> str | None:
        return keyring.get_password(self.service, f"{self.name}.{key}")

    def load(self) -> dict[str, Any] | None:
        count = self._get("count")
        if not count:
            return None
        try:
            parts = [self._get(str(i)) for i in range(int(count))]
            if any(p is None for p in parts):
                return None
            data = json.loads("".join(p for p in parts if p is not None))
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    def save(self, data: dict[str, Any]) -> None:
        self.clear()
        text = json.dumps(data, separators=(",", ":"))
        pieces = [text[i : i + _CHUNK] for i in range(0, len(text), _CHUNK)]
        for i, piece in enumerate(pieces):
            keyring.set_password(self.service, f"{self.name}.{i}", piece)
        keyring.set_password(self.service, f"{self.name}.count", str(len(pieces)))

    def clear(self) -> None:
        count = self._get("count")
        n = int(count) if count and count.isdigit() else 0
        for key in [*map(str, range(max(n, 0))), "count"]:
            with contextlib.suppress(PasswordDeleteError):
                keyring.delete_password(self.service, f"{self.name}.{key}")


@dataclass(frozen=True)
class RevokeOutcome:
    revoked: bool
    error: str | None = None


FlowRunner = Callable[[str, list[str]], dict[str, Any]]
HttpPost = Callable[[str, dict[str, str], float], int]


def _run_google_flow(client_secret_path: str, scopes: list[str]) -> dict[str, Any]:
    """Open the browser, let the user approve, and return what Google gave back."""
    flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, scopes)
    creds = flow.run_local_server(
        host="127.0.0.1",
        port=0,
        access_type="offline",
        prompt="consent",
        authorization_prompt_message="",
        success_message="Gmail is connected. You can close this tab and return to ContentShare.",
    )
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "expiry": creds.expiry.isoformat() if creds.expiry else None,
        "scopes": sorted(creds.granted_scopes or creds.scopes or scopes),
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "token_uri": creds.token_uri,
    }


def _http_post(url: str, data: dict[str, str], timeout: float) -> int:
    return requests.post(url, data=data, timeout=timeout).status_code


class GmailAuth:
    def __init__(
        self,
        store: TokenStore | None = None,
        *,
        flow_runner: FlowRunner = _run_google_flow,
        http_post: HttpPost = _http_post,
        credentials_factory: Callable[..., Any] | None = None,
        request_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.store = store or KeyringTokenStore()
        self._flow_runner = flow_runner
        self._http_post = http_post
        self._credentials_factory = credentials_factory
        self._request_factory = request_factory

    # ------------------------------------------------------------------ connect
    def connect(self, client_secret_path: str, scopes: list[str]) -> None:
        """Run the browser sign-in. Blocks until the user finishes or gives up."""
        if not Path(client_secret_path).is_file():
            raise AuthRequiredError(
                "The Google client file was not found. Download it from Google Cloud "
                "(OAuth client, type Desktop) and choose it in Settings."
            )
        try:
            data = self._flow_runner(client_secret_path, scopes)
        except Exception as exc:
            raise AuthRequiredError(f"Sign-in did not complete ({type(exc).__name__})") from exc
        if not data.get("refresh_token"):
            raise AuthRequiredError(
                "Google did not give long-term access. Remove ContentShare from your Google "
                "account's third-party access list and connect again."
            )
        missing = set(scopes) - set(data.get("scopes", []))
        if missing:
            raise AuthRequiredError(
                "Not all requested permissions were granted. ContentShare needs them all; "
                "please connect again and keep every box ticked."
            )
        self.store.save(data)
        log.info("Gmail connected")

    # ------------------------------------------------------------------ use
    def is_connected(self) -> bool:
        return self.store.load() is not None

    def credentials(self, scopes: list[str]) -> Any:
        """Ready-to-use credentials, refreshed if needed. Raises :class:`AuthRequiredError`
        when the user must connect again."""
        data = self.store.load()
        if not data or not data.get("refresh_token"):
            raise AuthRequiredError("Gmail is not connected")
        if not set(scopes) <= set(data.get("scopes", [])):
            raise AuthRequiredError("Gmail permissions changed; please connect again")
        creds = self._build(data, scopes)
        if not creds.valid:
            self._refresh(creds)
            data.update(
                token=creds.token, expiry=creds.expiry.isoformat() if creds.expiry else None
            )
            self.store.save(data)
        return creds

    def _build(self, data: dict[str, Any], scopes: list[str]) -> Any:
        factory = self._credentials_factory
        if factory is None:
            factory = Credentials
        expiry = datetime.fromisoformat(data["expiry"]) if data.get("expiry") else None
        return factory(
            token=data.get("token"),
            refresh_token=data["refresh_token"],
            token_uri=data.get("token_uri") or "https://oauth2.googleapis.com/token",
            client_id=data.get("client_id"),
            client_secret=data.get("client_secret"),
            scopes=scopes,
            expiry=expiry,
        )

    def _refresh(self, creds: Any) -> None:
        request = self._request_factory() if self._request_factory else _default_request()
        try:
            creds.refresh(request)
        except RefreshError as exc:  # access revoked, password changed, token expired for good
            raise AuthRequiredError(
                "Gmail access was revoked or expired; please connect again"
            ) from exc
        except TransportError as exc:
            raise TransientGmailError("Could not reach Google to refresh sign-in") from exc

    # ------------------------------------------------------------------ disconnect
    def disconnect(self) -> RevokeOutcome:
        """Revoke the token with Google, then delete the stored copy whatever happened."""
        data = self.store.load()
        outcome = RevokeOutcome(revoked=False, error="Not connected")
        if data:
            token = data.get("refresh_token") or data.get("token")
            try:
                status = self._http_post(REVOKE_URL, {"token": str(token)}, 10.0)
                outcome = RevokeOutcome(
                    status == 200, None if status == 200 else f"Google answered {status}"
                )
            except Exception as exc:
                outcome = RevokeOutcome(False, f"Could not reach Google ({type(exc).__name__})")
        self.store.clear()
        log.info("Gmail disconnected (revoked=%s)", outcome.revoked)
        return outcome


def _default_request() -> Any:
    return Request()

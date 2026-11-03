"""A small, careful wrapper over the Gmail API.

Everything the app does with Gmail goes through here: list ids, read a message, make and apply
a label, send a reply, look at a thread. Google's errors are turned into three kinds of our
own (sign in again / try later / not found), and temporary failures are retried with a pause.

Sending is never retried automatically: if the connection drops after Gmail accepted the
message, a blind retry would send the reply twice. The reply service decides what to do.
"""

from __future__ import annotations

import json
import logging
import socket
import time
from collections.abc import Callable, Sequence
from typing import Any

import httplib2
from googleapiclient.errors import HttpError

from contentshare.gmail.errors import (
    AuthRequiredError,
    GmailError,
    GmailNotFoundError,
    TransientGmailError,
)

log = logging.getLogger(__name__)

_PAGE = 100
_RATE_REASONS = {"ratelimitexceeded", "userratelimitexceeded", "backenderror"}
_TRANSIENT_STATUS = {429, 500, 502, 503, 504}


def _reason(exc: HttpError) -> str:
    try:
        payload = json.loads(exc.content.decode("utf-8", errors="replace"))
        error = payload.get("error", {})
        errors = error.get("errors") or [{}]
        return str(errors[0].get("reason") or error.get("status") or "").lower()
    except (ValueError, AttributeError, TypeError, IndexError):
        return ""


def map_error(exc: Exception) -> GmailError:
    """Turn a Google or network error into one of ours. Messages carry no request content."""
    if isinstance(exc, HttpError):
        status = int(exc.resp.status)
        reason = _reason(exc)
        if status == 404:
            return GmailNotFoundError("Not found in Gmail")
        if status == 401 or (status == 403 and reason not in _RATE_REASONS):
            return AuthRequiredError(f"Gmail refused access ({status} {reason}); connect again")
        if status in _TRANSIENT_STATUS or reason in _RATE_REASONS:
            return TransientGmailError(f"Gmail is busy or unavailable ({status} {reason})")
        return GmailError(f"Gmail rejected the request ({status} {reason})")
    if isinstance(exc, OSError | httplib2.HttpLib2Error | socket.timeout):
        return TransientGmailError(f"Network problem talking to Gmail ({type(exc).__name__})")
    return GmailError(f"Unexpected Gmail error ({type(exc).__name__})")


class GmailGateway:
    def __init__(
        self,
        service: Any,
        *,
        user_id: str = "me",
        max_attempts: int = 4,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._svc = service
        self._user = user_id
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._labels: dict[str, str] = {}

    # ------------------------------------------------------------------ plumbing
    def _execute(self, request: Any, *, retry: bool = True) -> Any:
        attempts = self._max_attempts if retry else 1
        for attempt in range(1, attempts + 1):
            try:
                return request.execute()
            except GmailError:
                raise
            except Exception as exc:
                mapped = map_error(exc)
                if isinstance(mapped, TransientGmailError) and attempt < attempts:
                    delay = 2 ** (attempt - 1)
                    log.warning("Gmail busy; retrying in %ss (%s)", delay, mapped)
                    self._sleep(delay)
                    continue
                raise mapped from exc
        raise AssertionError("unreachable")

    # ------------------------------------------------------------------ reading
    def profile_email(self) -> str:
        data = self._execute(self._svc.users().getProfile(userId=self._user))
        return str(data["emailAddress"])

    def list_message_ids(self, query: str, limit: int) -> list[str]:
        """Message ids matching ``query``, newest first, up to ``limit``."""
        ids: list[str] = []
        token: str | None = None
        while len(ids) < limit:
            request = self._svc.users().messages().list(
                userId=self._user, q=query, maxResults=min(_PAGE, limit - len(ids)), pageToken=token
            )  # fmt: skip
            page = self._execute(request)
            ids.extend(str(m["id"]) for m in page.get("messages", []))
            token = page.get("nextPageToken")
            if not token:
                break
        return ids[:limit]

    def get_message(self, message_id: str) -> dict[str, Any]:
        request = self._svc.users().messages().get(userId=self._user, id=message_id, format="full")
        return dict(self._execute(request))

    def thread_headers(self, thread_id: str, names: Sequence[str]) -> list[dict[str, Any]]:
        """For each message in a thread: its id, labels and the requested headers (lower-case)."""
        request = self._svc.users().threads().get(
            userId=self._user, id=thread_id, format="metadata", metadataHeaders=list(names)
        )  # fmt: skip
        thread = self._execute(request)
        out = []
        for message in thread.get("messages", []):
            headers = {
                str(h["name"]).lower(): str(h["value"])
                for h in message.get("payload", {}).get("headers", [])
            }
            out.append(
                {"id": message["id"], "labels": message.get("labelIds", []), "headers": headers}
            )
        return out

    # ------------------------------------------------------------------ labels
    def ensure_label(self, name: str) -> str:
        """The id of the label called ``name``, creating it if it does not exist."""
        if name in self._labels:
            return self._labels[name]
        found = self._find_label(name)
        if found is None:
            body = {
                "name": name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            }
            try:
                created = self._execute(
                    self._svc.users().labels().create(userId=self._user, body=body)
                )
                found = str(created["id"])
            except GmailError:
                found = self._find_label(name)  # someone created it a moment ago
                if found is None:
                    raise
        self._labels[name] = found
        return found

    def _find_label(self, name: str) -> str | None:
        data = self._execute(self._svc.users().labels().list(userId=self._user))
        for label in data.get("labels", []):
            if label.get("name") == name:
                return str(label["id"])
        return None

    def add_label(self, message_id: str, label_id: str) -> None:
        request = self._svc.users().messages().modify(
            userId=self._user, id=message_id, body={"addLabelIds": [label_id]}
        )  # fmt: skip
        self._execute(request)

    # ------------------------------------------------------------------ sending
    def send_raw(self, raw_base64url: str, thread_id: str | None) -> str:
        """Send a prepared message in a thread. One attempt only (see the module note)."""
        body: dict[str, str] = {"raw": raw_base64url}
        if thread_id:
            body["threadId"] = thread_id
        sent = self._execute(
            self._svc.users().messages().send(userId=self._user, body=body), retry=False
        )
        return str(sent["id"])

"""An in-memory stand-in for the Gmail API, shaped like ``googleapiclient``'s resource chain
(``service.users().messages().list(...).execute()``), so the real adapter code runs against it.
No network is ever used."""

from __future__ import annotations

import base64
import email
import itertools
import json
import re
from collections.abc import Callable, Sequence
from email import policy
from typing import Any

import httplib2
from googleapiclient.errors import HttpError

GOOD_AUTH_SERVER = "mx.google.com"


def http_error(status: int, reason: str = "", message: str = "error") -> HttpError:
    resp = httplib2.Response({"status": str(status)})
    body = {"error": {"code": status, "message": message, "errors": [{"reason": reason}]}}
    return HttpError(resp, json.dumps(body).encode("utf-8"))


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def auth_header(
    domain: str,
    *,
    dmarc: str = "pass",
    spf: str = "pass",
    dkim: str = "pass",
    server: str = GOOD_AUTH_SERVER,
) -> str:
    return (
        f"{server}; dkim={dkim} header.i=@{domain} header.s=s1; "
        f"spf={spf} (google.com: domain of someone@{domain} designates 1.2.3.4 as permitted "
        f"sender) smtp.mailfrom=someone@{domain}; "
        f"dmarc={dmarc} (p=NONE sp=NONE dis=NONE) header.from={domain}"
    )


_ids = itertools.count(1)


def make_message(
    *,
    message_id: str | None = None,
    thread_id: str | None = None,
    from_: str = "Anita R. <anita@dept.org>",
    subject: str = "Request",
    body: str = "",
    html: str | None = None,
    auth: str | list[str] | None = "default",
    internal_ms: int = 1_793_000_000_000,
    labels: Sequence[str] = ("INBOX",),
    attachments: Sequence[tuple[str, str]] = (),
    rfc_id: str | None = None,
    references: str | None = None,
    extra_headers: Sequence[tuple[str, str]] = (),
) -> dict[str, Any]:
    n = next(_ids)
    message_id = message_id or f"m{n}"
    thread_id = thread_id or f"t{n}"
    headers = [
        {"name": "From", "value": from_},
        {"name": "Subject", "value": subject},
        {"name": "Message-ID", "value": rfc_id or f"<orig{n}@mail.dept.org>"},
    ]
    if references:
        headers.append({"name": "References", "value": references})
    if auth == "default":
        domain = re.search(r"@([\w.\-]+)", from_)
        auth = auth_header(domain.group(1) if domain else "dept.org")
    for value in [auth] if isinstance(auth, str) else (auth or []):
        headers.append({"name": "Authentication-Results", "value": value})
    headers += [{"name": k, "value": v} for k, v in extra_headers]

    def part(mime: str, text: str) -> dict[str, Any]:
        return {"mimeType": mime, "body": {"data": b64(text)}, "headers": []}

    parts: list[dict[str, Any]] = []
    if body or not html:
        parts.append(part("text/plain", body))
    if html:
        parts.append(part("text/html", html))
    payload: dict[str, Any]
    if len(parts) == 1 and not attachments:
        payload = {**parts[0], "headers": headers}
    else:
        children: list[dict[str, Any]] = (
            [{"mimeType": "multipart/alternative", "parts": parts}] if len(parts) > 1 else parts
        )
        for filename, content in attachments:
            children.append(
                {
                    "mimeType": "application/octet-stream",
                    "filename": filename,
                    "body": {"attachmentId": f"att-{filename}", "size": len(content)},
                }
            )
        payload = {"mimeType": "multipart/mixed", "headers": headers, "parts": children}
    return {
        "id": message_id,
        "threadId": thread_id,
        "labelIds": list(labels),
        "internalDate": str(internal_ms),
        "payload": payload,
    }


class _Request:
    def __init__(self, gmail: FakeGmail, name: str, fn: Callable[[], Any]) -> None:
        self._gmail, self._name, self._fn = gmail, name, fn

    def execute(self) -> Any:
        self._gmail.calls.append(self._name)
        queue = self._gmail.failures.get(self._name)
        if queue:
            raise queue.pop(0)
        return self._fn()


class FakeGmail:
    def __init__(self, address: str = "me@org.org", page_size: int = 100) -> None:
        self.address = address
        self.page_size = page_size
        self.messages: dict[str, dict[str, Any]] = {}
        self.labels: dict[str, str] = {}
        self.sent: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self.failures: dict[str, list[Exception]] = {}
        self.queries: list[str] = []
        self.loose_from_match = False  # mimic Gmail matching "dept.org" inside "notdept.org"
        self.drop_response_on_send = False  # message is stored, but the caller sees a timeout
        self.hide_thread = False  # make thread lookups fail with a network error

    # ------------------------------------------------------------------ test helpers
    def add(self, message: dict[str, Any]) -> dict[str, Any]:
        self.messages[message["id"]] = message
        return message

    def fail(self, method: str, *errors: Exception) -> None:
        self.failures.setdefault(method, []).extend(errors)

    def label_ids_of(self, message_id: str) -> list[str]:
        return list(self.messages[message_id]["labelIds"])

    # ------------------------------------------------------------------ the API surface
    def users(self) -> _Users:
        return _Users(self)


class _Users:
    def __init__(self, gmail: FakeGmail) -> None:
        self.g = gmail

    def getProfile(self, userId: str) -> _Request:
        return _Request(self.g, "getProfile", lambda: {"emailAddress": self.g.address})

    def messages(self) -> _Messages:
        return _Messages(self.g)

    def labels(self) -> _Labels:
        return _Labels(self.g)

    def threads(self) -> _Threads:
        return _Threads(self.g)


class _Messages:
    def __init__(self, gmail: FakeGmail) -> None:
        self.g = gmail

    def list(
        self, userId: str, q: str = "", maxResults: int = 100, pageToken: str | None = None
    ) -> _Request:
        def run() -> dict[str, Any]:
            self.g.queries.append(q)
            matches = [m for m in self.g.messages.values() if self._matches(m, q)]
            matches.sort(key=lambda m: -int(m["internalDate"]))
            start = int(pageToken or 0)
            size = min(maxResults, self.g.page_size)
            page = matches[start : start + size]
            out: dict[str, Any] = {
                "messages": [{"id": m["id"], "threadId": m["threadId"]} for m in page]
            }
            if start + size < len(matches):
                out["nextPageToken"] = str(start + size)
            return out

        return _Request(self.g, "messages.list", run)

    def _matches(self, message: dict[str, Any], q: str) -> bool:
        if "SENT" in message["labelIds"] and "in:sent" not in q:
            pass  # sent mail is still listed unless the query says otherwise
        senders = re.search(r"(?<![-\w])from:\(([^)]*)\)", q)
        for excluded_senders in re.findall(r"-from:\(([^)]*)\)", q):
            tokens = [t for t in re.split(r"\s+OR\s+", excluded_senders.strip()) if t]
            if any(t in self._from_address(message) for t in tokens):
                return False
        if senders:
            tokens = [t for t in re.split(r"\s+OR\s+", senders.group(1).strip()) if t]
            sender = self._from_address(message)
            if not any(self._token_matches(t, sender) for t in tokens):
                return False
        for excluded in re.findall(r"-label:(\S+)", q):
            label_ids = {
                i for i, name in self.g.labels.items() if self._search_name(name) == excluded
            }
            if label_ids & set(message["labelIds"]):
                return False
        return True

    def _token_matches(self, token: str, sender: str) -> bool:
        if "@" in token:
            return sender == token
        return token in sender if self.g.loose_from_match else sender.endswith("@" + token)

    @staticmethod
    def _search_name(name: str) -> str:
        return name.lower().replace("/", "-").replace(" ", "-")

    @staticmethod
    def _from_address(message: dict[str, Any]) -> str:
        for h in message["payload"]["headers"]:
            if h["name"].lower() == "from":
                value = h["value"]
                angled = re.findall(r"<([^<>]+)>", value)  # the real address, not a quoted name
                if angled:
                    return angled[-1].strip().lower()
                found = re.search(r"[\w.%+\-']+@[\w.\-]+", value)
                return found.group(0).lower() if found else ""
        return ""

    def get(self, userId: str, id: str, format: str = "full") -> _Request:
        def run() -> dict[str, Any]:
            if id not in self.g.messages:
                raise http_error(404, "notFound")
            return json.loads(json.dumps(self.g.messages[id]))

        return _Request(self.g, "messages.get", run)

    def modify(self, userId: str, id: str, body: dict[str, Any]) -> _Request:
        def run() -> dict[str, Any]:
            message = self.g.messages[id]
            for label in body.get("addLabelIds", []):
                if label not in message["labelIds"]:
                    message["labelIds"].append(label)
            return {"id": id}

        return _Request(self.g, "messages.modify", run)

    def send(self, userId: str, body: dict[str, Any]) -> _Request:
        def run() -> dict[str, Any]:
            raw = base64.urlsafe_b64decode(body["raw"] + "=" * (-len(body["raw"]) % 4))
            parsed = email.message_from_bytes(raw, policy=policy.SMTP)
            sent_id = f"sent{len(self.g.sent) + 1}"
            headers = [{"name": k, "value": str(v)} for k, v in parsed.items()]
            thread = body.get("threadId") or f"t-{sent_id}"
            self.g.sent.append({"id": sent_id, "threadId": thread, "message": parsed, "raw": raw})
            self.g.messages[sent_id] = {
                "id": sent_id,
                "threadId": thread,
                "labelIds": ["SENT"],
                "internalDate": "1793000001000",
                "payload": {"mimeType": "text/plain", "headers": headers, "body": {}},
            }
            if self.g.drop_response_on_send:
                self.g.drop_response_on_send = False
                raise TimeoutError("connection dropped after the message was accepted")
            return {"id": sent_id, "threadId": thread, "labelIds": ["SENT"]}

        return _Request(self.g, "messages.send", run)


class _Labels:
    def __init__(self, gmail: FakeGmail) -> None:
        self.g = gmail

    def list(self, userId: str) -> _Request:
        return _Request(
            self.g,
            "labels.list",
            lambda: {"labels": [{"id": i, "name": n} for i, n in self.g.labels.items()]},
        )

    def create(self, userId: str, body: dict[str, Any]) -> _Request:
        def run() -> dict[str, Any]:
            if body["name"] in self.g.labels.values():
                raise http_error(409, "duplicate", "Label name exists")
            label_id = f"Label_{len(self.g.labels) + 1}"
            self.g.labels[label_id] = body["name"]
            return {"id": label_id, "name": body["name"]}

        return _Request(self.g, "labels.create", run)


class _Threads:
    def __init__(self, gmail: FakeGmail) -> None:
        self.g = gmail

    def get(
        self, userId: str, id: str, format: str = "metadata", metadataHeaders: Sequence[str] = ()
    ) -> _Request:
        def run() -> dict[str, Any]:
            if self.g.hide_thread:
                raise OSError("network is down")
            wanted = {h.lower() for h in metadataHeaders}
            messages = []
            for m in self.g.messages.values():
                if m["threadId"] != id:
                    continue
                headers = [h for h in m["payload"]["headers"] if h["name"].lower() in wanted]
                messages.append(
                    {"id": m["id"], "labelIds": m["labelIds"], "payload": {"headers": headers}}
                )
            return {"id": id, "messages": messages}

        return _Request(self.g, "threads.get", run)

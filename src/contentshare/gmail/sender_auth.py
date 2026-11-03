"""Deciding whether a sender really is who the From header says (SPF / DKIM / DMARC).

Gmail checks every incoming message and records the outcome in an ``Authentication-Results``
header. We trust that header only when it was written by one of our trusted servers, and then
only the topmost one: the receiving server adds its own result at the top, while anything a
sender wrote themselves is further down. A result also only counts if it is about the sender's
own domain, so a pass for some other domain proves nothing.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from enum import StrEnum


class Verdict(StrEnum):
    VERIFIED = "verified"
    SUSPICIOUS = "suspicious"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AuthResult:
    verdict: Verdict
    reason: str


_COMMENT = re.compile(r"\([^()]*\)")
_METHOD = re.compile(
    r"^(?P<method>[a-z0-9\-]+)\s*=\s*(?P<result>[a-z]+)(?P<rest>.*)$", re.IGNORECASE | re.DOTALL
)
_PROP = re.compile(r"(?P<key>[a-z0-9\-]+\.[a-z0-9\-]+)\s*=\s*(?P<value>[^\s;]+)", re.IGNORECASE)


@dataclass(frozen=True)
class _Check:
    method: str
    result: str
    props: dict[str, str]


def _parse(header: str) -> tuple[str, list[_Check]]:
    segments = [s.strip() for s in _COMMENT.sub(" ", header).split(";")]
    server = segments[0].split()[0].lower() if segments and segments[0] else ""
    checks = []
    for segment in segments[1:]:
        match = _METHOD.match(segment)
        if match:
            props = {
                m["key"].lower(): m["value"].strip('<>"').lower()
                for m in _PROP.finditer(match["rest"])
            }
            checks.append(_Check(match["method"].lower(), match["result"].lower(), props))
    return server, checks


def _domain_of(value: str) -> str:
    return value.rpartition("@")[2].lower().strip(".")


def aligned(authenticated: str, sender_domain: str) -> bool:
    """Same domain, or one is a subdomain of the other (relaxed alignment). Without a public
    suffix list, the shorter name must still contain a dot, so ``org`` aligns with nothing."""
    a, b = authenticated.lower().strip("."), sender_domain.lower().strip(".")
    if not a or not b:
        return False
    if a == b:
        return True
    longer, shorter = (a, b) if len(a) > len(b) else (b, a)
    return longer.endswith("." + shorter) and "." in shorter


def _passes(checks: list[_Check], method: str, sender_domain: str, *keys: str) -> bool:
    return any(
        check.method == method
        and check.result == "pass"
        and any(
            k in check.props and aligned(_domain_of(check.props[k]), sender_domain) for k in keys
        )
        for check in checks
    )


def verify_sender(
    auth_headers: Sequence[str],
    sender_domain: str,
    *,
    trusted_servers: Collection[str] = ("mx.google.com",),
    require: bool = True,
) -> AuthResult:
    if not require:
        return AuthResult(Verdict.VERIFIED, "Authentication checks are turned off in settings")
    trusted = {s.lower() for s in trusted_servers}
    for header in auth_headers:  # top to bottom: the first trusted one is the receiver's own
        server, checks = _parse(header)
        if server not in trusted:
            continue
        if _passes(checks, "dmarc", sender_domain, "header.from"):
            return AuthResult(Verdict.VERIFIED, "DMARC passed for the sender's domain")
        if _passes(checks, "spf", sender_domain, "smtp.mailfrom", "smtp.helo") and _passes(
            checks, "dkim", sender_domain, "header.d", "header.i"
        ):
            return AuthResult(Verdict.VERIFIED, "SPF and DKIM passed for the sender's domain")
        summary = ", ".join(f"{c.method}={c.result}" for c in checks) or "no results"
        return AuthResult(Verdict.SUSPICIOUS, f"Sender could not be authenticated ({summary})")
    return AuthResult(Verdict.UNKNOWN, "No authentication result from a trusted mail server")

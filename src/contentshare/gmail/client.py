"""Connecting the app to the real Gmail. Everything else in this package works against the
:class:`~contentshare.gmail.gateway.GmailGateway` this builds, which is also what the tests
replace with a fake."""

from __future__ import annotations

from googleapiclient.discovery import build

from contentshare.config.models import AppConfig
from contentshare.gmail.auth import GmailAuth, required_scopes
from contentshare.gmail.gateway import GmailGateway


def build_gateway(auth: GmailAuth, config: AppConfig) -> GmailGateway:
    """A gateway using the saved sign-in. Raises ``AuthRequiredError`` if the user needs to
    connect (or reconnect) Gmail first."""
    credentials = auth.credentials(required_scopes(config.gmail))
    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    return GmailGateway(service)

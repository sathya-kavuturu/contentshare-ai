"""Errors raised by the Gmail layer. Messages never contain tokens or email bodies."""

from __future__ import annotations


class GmailError(Exception):
    """Something went wrong talking to Gmail."""


class AuthRequiredError(GmailError):
    """Not connected, access was revoked, or the granted permissions changed. The user has to
    connect Gmail again."""


class TransientGmailError(GmailError):
    """Rate limit, server error or network trouble: worth trying again later."""


class GmailNotFoundError(GmailError):
    """The message or thread no longer exists (for example it was deleted)."""


class MessageFormatError(GmailError):
    """An email is not in a shape we can use (for example no usable From address)."""

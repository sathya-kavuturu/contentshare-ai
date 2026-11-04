"""People who use the app: a name and a password each, kept in the local database.

This is for knowing who did what (the History shows who approved a request), not for keeping
determined people out: anyone who can read the database file on this PC can read the hashes.
A password can be anything, even one letter. It is never stored or logged: only a salted
scrypt hash is kept.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3

from contentshare.storage.repository import Repository

NAME_MAX = 40
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "maxmem": 64 * 1024 * 1024}


class UserError(Exception):
    """The message is written for the person and shown as it is."""


def clean_name(text: str, limit: int = NAME_MAX) -> str:
    """One line, no control characters, at most ``limit`` long."""
    return " ".join("".join(c for c in text if c.isprintable()).split())[:limit]


def hash_password(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT).hex()


class UserStore:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    def names(self) -> list[str]:
        return self.repo.list_user_names()

    def create(self, name: str, password: str) -> str:
        """Add a user. Returns the name as stored."""
        cleaned = clean_name(name)
        if not cleaned:
            raise UserError("Type a name")
        if not password:
            raise UserError("Type a password (any letters or numbers will do)")
        salt = secrets.token_bytes(16)
        try:
            added = self.repo.add_user(cleaned, salt.hex(), hash_password(password, salt))
        except sqlite3.IntegrityError:
            added = False
        if not added:
            raise UserError(f"There is already a user called “{cleaned}”")
        return cleaned

    def verify(self, name: str, password: str) -> str | None:
        """The user's name as stored if the password is right, else None. The time taken does
        not depend on which part was wrong."""
        row = self.repo.get_user(clean_name(name))
        salt = bytes.fromhex(row["salt"]) if row else b"\0" * 16
        expected = row["pw_hash"] if row else "0" * 64
        ok = hmac.compare_digest(hash_password(password, salt), expected)
        if row is None or not ok:
            return None
        self.repo.touch_login(str(row["name"]))
        return str(row["name"])

"""Small admin CLI: set up files, and connect / check Gmail until the app screens exist."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from contentshare.config.store import (
    ConfigError,
    data_dir,
    default_config_path,
    load_config,
    save_config,
)
from contentshare.gmail.auth import GmailAuth, required_scopes
from contentshare.gmail.client import build_gateway
from contentshare.gmail.errors import GmailError
from contentshare.gmail.intake import peek
from contentshare.storage.db import Database


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="contentshare-admin")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create a default config and the database if missing")
    check = sub.add_parser("check-config", help="validate a config file")
    check.add_argument("path", nargs="?", type=Path, default=None)
    sub.add_parser("gmail-connect", help="sign in to Gmail in your browser")
    sub.add_parser("gmail-disconnect", help="revoke Gmail access and forget the saved sign-in")
    sub.add_parser("gmail-status", help="show whether Gmail is connected")
    look = sub.add_parser(
        "gmail-peek",
        help="read-only: show recent mail from your department senders and how each is judged",
    )
    look.add_argument("--limit", type=int, default=10)
    return parser


def _init() -> int:
    cfg_path = default_config_path()
    if not cfg_path.exists():
        save_config(load_config(cfg_path), cfg_path)
        print(f"Created {cfg_path}")
    db = Database(data_dir() / "contentshare.db")
    print(f"Database ready at {db.path}")
    return 0


def _gmail(command: str, limit: int) -> int:
    try:
        config = load_config()
        auth = GmailAuth()
        if command == "gmail-connect":
            if not config.gmail.client_secret_path:
                print("Set gmail.client_secret_path in the settings file first.", file=sys.stderr)
                return 1
            auth.connect(config.gmail.client_secret_path, required_scopes(config.gmail))
            print("Gmail connected.")
        elif command == "gmail-disconnect":
            outcome = auth.disconnect()
            print(
                "Access revoked at Google." if outcome.revoked else f"Not revoked: {outcome.error}"
            )
            print("The saved sign-in on this PC was deleted.")
        elif command == "gmail-status":
            print("Connected." if auth.is_connected() else "Not connected.")
        else:  # gmail-peek
            rows = peek(build_gateway(auth, config), config, limit)
            for row in rows:
                who = row.department or "NOT IN ANY DEPARTMENT"
                print(
                    f"{row.sender} [{who}] {row.verdict.upper()}: {row.reason}\n    {row.subject}"
                )
            print(f"{len(rows)} message(s) shown. Nothing was saved or changed.")
    except (ConfigError, GmailError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "init":
        return _init()
    if args.command.startswith("gmail-"):
        return _gmail(args.command, getattr(args, "limit", 10))
    try:
        config = load_config(args.path)
    except ConfigError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    print(f"OK: {len(config.departments)} department(s), {len(config.source_roots)} source root(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

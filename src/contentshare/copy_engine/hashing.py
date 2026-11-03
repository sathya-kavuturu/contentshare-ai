"""Hash algorithms and checksum-manifest naming."""

from __future__ import annotations

import hashlib
from typing import Protocol

import blake3
import xxhash

ALGORITHMS = ("xxh3_128", "blake3", "sha256")


class Hasher(Protocol):
    def update(self, data: bytes | memoryview, /) -> object: ...
    def hexdigest(self) -> str: ...


def new_hasher(algorithm: str) -> Hasher:
    if algorithm == "xxh3_128":
        return xxhash.xxh3_128()
    if algorithm == "blake3":
        return blake3.blake3()
    if algorithm == "sha256":
        return hashlib.sha256()
    raise ValueError(f"Unknown hash algorithm: {algorithm!r}")


def manifest_suffix(algorithm: str) -> str:
    """File extension of the checksum file, matching the format each standard tool reads
    (``xxhsum -c``, ``b3sum --check``, ``sha256sum -c``)."""
    return {"xxh3_128": ".xxhash", "blake3": ".b3sum", "sha256": ".sha256sum"}[algorithm]


def manifest_line(algorithm: str, digest: str, rel_path: str) -> str:
    marker = " *" if algorithm == "sha256" else "  "  # sha256sum's binary-mode marker
    return f"{digest}{marker}{rel_path}\n"

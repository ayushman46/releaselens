"""Streaming file digests (never loads a whole file into memory)."""

from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK = 4 * 1024 * 1024


def file_digest(path: Path, algorithm: str) -> str:
    h = hashlib.new(algorithm, usedforsecurity=algorithm != "md5")
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def md5_file(path: Path) -> str:
    """MD5 is used only to match the publisher's checksum (Figshare), never for security."""
    return file_digest(path, "md5")


def sha256_file(path: Path) -> str:
    return file_digest(path, "sha256")

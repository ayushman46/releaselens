"""API key format, hashing and creation (ADR-0016).

Format: ``rlk_<prefix>_<secret>``.

* ``rlk_``: a fixed marker, so secret scanners (gitleaks, GitHub push protection) can match
  leaked keys with a simple pattern.
* ``prefix``: 12 characters from ``[A-Za-z0-9]``, stored in clear under a unique index and
  shown in the UI to tell keys apart. It is the database lookup handle. Because the
  alphabet has no ``_``, the key splits unambiguously.
* ``secret``: 32 random bytes from :mod:`secrets`, URL-safe base64 without padding
  (43 characters).

Only ``sha256(full key)`` is stored. A fast hash is the right tool here, unlike for
passwords: the input carries 256 bits of entropy, so a stolen hash cannot be brute-forced,
and a slow KDF would add latency to every ingest request for no security gain. User
passwords (M10) use Argon2id. Hashes are compared with :func:`hmac.compare_digest`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import string
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from rl_core.tenancy import Principal, TenantContext
from rl_db.repositories import insert_api_key

KEY_MARKER: Final = "rlk_"
PREFIX_LEN: Final = 12
SECRET_BYTES: Final = 32
_PREFIX_ALPHABET: Final = string.ascii_letters + string.digits
_KEY_RE: Final = re.compile(r"^rlk_([A-Za-z0-9]{12})_([A-Za-z0-9_-]{43})$")
MAX_PRESENTED_KEY_LEN: Final = 128  # anything longer is rejected before any work is done

# Must match the CHECK constraint ck_api_keys_scopes_known (migration 0001).
KNOWN_SCOPES: Final = frozenset({"deployments:write", "telemetry:write", "risk:write", "read"})
MAX_KEY_NAME_LEN: Final = 120


@dataclass(frozen=True, slots=True)
class GeneratedKey:
    prefix: str
    secret_hash: str
    plaintext: str = field(repr=False)  # never logged, never stored


def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_api_key() -> GeneratedKey:
    prefix = "".join(secrets.choice(_PREFIX_ALPHABET) for _ in range(PREFIX_LEN))
    secret = base64.urlsafe_b64encode(secrets.token_bytes(SECRET_BYTES)).rstrip(b"=").decode()
    plaintext = f"{KEY_MARKER}{prefix}_{secret}"
    return GeneratedKey(prefix=prefix, secret_hash=hash_api_key(plaintext), plaintext=plaintext)


def parse_prefix(presented: str) -> str | None:
    """The lookup prefix of a well-formed key, else None (malformed keys never hit the DB)."""
    if len(presented) > MAX_PRESENTED_KEY_LEN:
        return None
    match = _KEY_RE.fullmatch(presented)
    return match.group(1) if match else None


def verify_api_key(presented: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_api_key(presented), stored_hash)


def normalize_scopes(scopes: Iterable[str]) -> frozenset[str]:
    cleaned = frozenset(s.strip() for s in scopes if s.strip())
    if not cleaned:
        raise ValueError("at least one scope is required")
    unknown = cleaned - KNOWN_SCOPES
    if unknown:
        raise ValueError(f"unknown scope(s): {sorted(unknown)}; known: {sorted(KNOWN_SCOPES)}")
    return cleaned


@dataclass(frozen=True, slots=True)
class CreatedApiKey:
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    prefix: str
    scopes: frozenset[str]
    expires_at: datetime | None
    plaintext: str = field(repr=False)  # returned exactly once, to the creator


async def create_api_key(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    name: str,
    scopes: Iterable[str],
    *,
    expires_at: datetime | None = None,
    created_by: uuid.UUID | None = None,
) -> CreatedApiKey:
    """Create a key for ``tenant_id`` and return its plaintext, which nothing can recover later.

    The caller commits. A prefix collision (62**12 possible prefixes) surfaces as an
    IntegrityError on ``uq_api_keys_prefix`` rather than being retried silently.
    """
    name = name.strip()
    if not 1 <= len(name) <= MAX_KEY_NAME_LEN:
        raise ValueError(f"name must be 1..{MAX_KEY_NAME_LEN} characters")
    if expires_at is not None and expires_at.tzinfo is None:
        raise ValueError("expires_at must be timezone-aware")
    clean_scopes = normalize_scopes(scopes)
    generated = generate_api_key()
    ctx = TenantContext(tenant_id=tenant_id, principal=Principal.SYSTEM, actor_id=created_by)
    row = await insert_api_key(
        session,
        ctx,
        name=name,
        prefix=generated.prefix,
        secret_hash=generated.secret_hash,
        scopes=clean_scopes,
        expires_at=expires_at,
        created_by=created_by,
    )
    return CreatedApiKey(
        id=row.id,
        tenant_id=tenant_id,
        name=name,
        prefix=generated.prefix,
        scopes=clean_scopes,
        expires_at=expires_at,
        plaintext=generated.plaintext,
    )

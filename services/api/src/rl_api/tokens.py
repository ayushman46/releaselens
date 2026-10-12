"""Random secrets handed to browsers and e-mail inboxes, and how they are stored.

Every token is 256 bits from :mod:`secrets` behind a short type marker (``rla_`` access,
``rlr_`` refresh, ``rlv_`` verify e-mail, ``rlp_`` password reset, ``rli_`` invitation,
``rls_`` OAuth state). The marker lets secret scanners recognise a leaked token and lets the
server reject a token of the wrong kind before any lookup. Only ``sha256(token)`` is stored
(same reasoning as ADR-0016: the input has 256 bits of entropy, so a fast hash is safe).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import uuid
from typing import Final

ACCESS: Final = "rla_"
REFRESH: Final = "rlr_"
VERIFY_EMAIL: Final = "rlv_"
RESET_PASSWORD: Final = "rlp_"  # noqa: S105 - a token marker, not a password
INVITE: Final = "rli_"
OAUTH_STATE: Final = "rls_"

_TOKEN_RE: Final = re.compile(r"^rl[arvpis]_[A-Za-z0-9_-]{43}$")
MAX_TOKEN_LEN: Final = 64


def new_token(marker: str) -> str:
    return f"{marker}{secrets.token_urlsafe(32)}"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def well_formed(token: str | None, marker: str) -> bool:
    """Cheap shape check before any database work (also bounds the input size)."""
    return (
        token is not None
        and len(token) <= MAX_TOKEN_LEN
        and token.startswith(marker)
        and _TOKEN_RE.fullmatch(token) is not None
    )


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def csrf_token_for(key: bytes, session_id: uuid.UUID) -> str:
    """The session's CSRF token: HMAC-SHA256(csrf key, session id). Deterministic, so it
    survives refreshes without being stored, and useless for any other session."""
    return b64url(hmac.new(key, b"csrf:" + session_id.bytes, hashlib.sha256).digest())


def pkce_pair() -> tuple[str, str]:
    """``(code_verifier, code_challenge)`` for PKCE S256 (RFC 7636)."""
    verifier = secrets.token_urlsafe(64)  # 86 characters, inside the 43..128 range
    challenge = b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def constant_time_equal(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))

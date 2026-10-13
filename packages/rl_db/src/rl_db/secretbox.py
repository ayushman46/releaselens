"""Authenticated encryption for secrets stored in the database (proposed ADR "alert URL
encryption"; docs/security/AUTH.md §7).

A Slack incoming-webhook URL *is* a credential: anyone holding it can post into the
customer's channel. It is stored only as ciphertext produced here:

* **AES-256-GCM** (``cryptography``'s AEAD), a fresh 96-bit random nonce per encryption.
* The key is derived from ``RL_SECRET_KEY`` with **HKDF-SHA256** and a purpose label, so the
  one operator secret never doubles as a raw key and other purposes (CSRF tokens) get
  independent keys.
* **Associated data** binds a ciphertext to its row (for example
  ``alert_rules.slack_webhook_url:<tenant_id>:<rule_id>``). A ciphertext copied into another
  tenant's row, or another column, fails authentication instead of decrypting.
* **Rotation:** the token names its key (``kid`` = first 8 hex characters of SHA-256 of the
  derived key). Old master keys listed in ``RL_SECRET_KEY_PREVIOUS`` still decrypt; new
  ciphertexts always use the current key, and :meth:`SecretBox.needs_reencrypt` tells a
  re-encryption job which rows to rewrite.

Token format: ``v1.<kid>.<base64url(nonce || ciphertext+tag)>``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
from collections.abc import Iterable
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

VERSION: Final = "v1"
# The master key the API and worker fall back to when RL_ENV is dev or test and
# RL_SECRET_KEY is unset. Public by design: it protects only local throwaway data, and both
# services refuse it when RL_ENV=prod.
DEV_MASTER_KEY: Final = "releaselens-dev-only-secret-key-not-for-production"  # gitleaks:allow
MIN_MASTER_KEY_LEN: Final = 32
_NONCE_BYTES: Final = 12
_ENCRYPTION_LABEL: Final = b"releaselens/secretbox/aes-256-gcm/v1"


class SecretBoxError(Exception):
    """A ciphertext could not be decrypted (tampered, wrong row, unknown or rotated-out key)."""


def derive_key(master_key: str, label: str | bytes, length: int = 32) -> bytes:
    """A purpose-specific key from the operator's master secret (HKDF-SHA256)."""
    if len(master_key) < MIN_MASTER_KEY_LEN:
        raise ValueError(f"the master key must be at least {MIN_MASTER_KEY_LEN} characters")
    info = label.encode() if isinstance(label, str) else label
    hkdf = HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=info)
    return hkdf.derive(master_key.encode("utf-8"))


def _kid(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:8]


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class SecretBox:
    def __init__(self, master_key: str, previous_keys: Iterable[str] = ()) -> None:
        current = derive_key(master_key, _ENCRYPTION_LABEL)
        self._current_kid = _kid(current)
        self._keys: dict[str, AESGCM] = {self._current_kid: AESGCM(current)}
        for old in previous_keys:
            if not old:
                continue
            key = derive_key(old, _ENCRYPTION_LABEL)
            self._keys.setdefault(_kid(key), AESGCM(key))

    @property
    def current_kid(self) -> str:
        return self._current_kid

    def encrypt(self, plaintext: str, *, context: str) -> str:
        nonce = os.urandom(_NONCE_BYTES)
        sealed = self._keys[self._current_kid].encrypt(
            nonce, plaintext.encode("utf-8"), context.encode("utf-8")
        )
        return f"{VERSION}.{self._current_kid}.{_b64e(nonce + sealed)}"

    def decrypt(self, token: str, *, context: str) -> str:
        try:
            version, kid, body = token.split(".", 2)
        except ValueError as exc:
            raise SecretBoxError("malformed ciphertext") from exc
        if version != VERSION:
            raise SecretBoxError(f"unsupported ciphertext version {version!r}")
        aead = self._keys.get(kid)
        if aead is None:
            raise SecretBoxError("ciphertext was sealed with an unknown key")
        try:
            raw = _b64d(body)
        except (binascii.Error, ValueError) as exc:
            raise SecretBoxError("malformed ciphertext") from exc
        if len(raw) <= _NONCE_BYTES:
            raise SecretBoxError("malformed ciphertext")
        try:
            plain = aead.decrypt(raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], context.encode("utf-8"))
        except InvalidTag as exc:
            raise SecretBoxError("ciphertext failed authentication") from exc
        return plain.decode("utf-8")

    def needs_reencrypt(self, token: str) -> bool:
        """True if ``token`` was sealed with a previous key."""
        parts = token.split(".", 2)
        return len(parts) != 3 or parts[1] != self._current_kid


def slack_webhook_context(tenant_id: object, rule_id: object) -> str:
    """Associated data for an alert rule's Slack webhook URL."""
    return f"alert_rules.slack_webhook_url:{tenant_id}:{rule_id}"

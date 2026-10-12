"""API key format, hashing and scope validation (no database)."""

from __future__ import annotations

import hashlib
import re

import pytest

from rl_api.apikeys import (
    KNOWN_SCOPES,
    generate_api_key,
    hash_api_key,
    normalize_scopes,
    parse_prefix,
    verify_api_key,
)

KEY_FORMAT = re.compile(r"^rlk_[A-Za-z0-9]{12}_[A-Za-z0-9_-]{43}$")


def test_generated_keys_have_the_documented_format() -> None:
    keys = [generate_api_key() for _ in range(200)]
    for key in keys:
        assert KEY_FORMAT.fullmatch(key.plaintext)
        assert key.plaintext[4:16] == key.prefix
        assert key.secret_hash == hashlib.sha256(key.plaintext.encode()).hexdigest()
    assert len({k.plaintext for k in keys}) == len(keys)
    assert len({k.prefix for k in keys}) == len(keys)


def test_plaintext_is_kept_out_of_repr() -> None:
    key = generate_api_key()
    assert key.plaintext not in repr(key)
    assert key.prefix in repr(key)


def test_parse_prefix_accepts_only_well_formed_keys() -> None:
    key = generate_api_key()
    assert parse_prefix(key.plaintext) == key.prefix
    # Secrets may contain "_" and "-" (URL-safe base64); the fixed prefix length keeps
    # parsing unambiguous.
    assert parse_prefix("rlk_abcdefABCDEF_" + "_-" * 21 + "x") == "abcdefABCDEF"
    for bad in [
        "",
        "rlk_",
        key.plaintext[:-1],
        key.plaintext + "x",
        key.plaintext.replace("rlk_", "rlx_"),
        "rlk_abc_def",
        "rlk_" + "a" * 12 + "_" + "a" * 42 + "=",
        "x" * 10_000,
        key.plaintext + "\n",
    ]:
        assert parse_prefix(bad) is None, bad


def test_verify_uses_the_full_key() -> None:
    key = generate_api_key()
    assert verify_api_key(key.plaintext, key.secret_hash)
    tampered = key.plaintext[:-1] + ("A" if key.plaintext[-1] != "A" else "B")
    assert not verify_api_key(tampered, key.secret_hash)
    assert hash_api_key(key.plaintext) == key.secret_hash


def test_normalize_scopes() -> None:
    assert normalize_scopes([" read ", "read", "deployments:write"]) == {
        "read",
        "deployments:write",
    }
    assert normalize_scopes(KNOWN_SCOPES) == KNOWN_SCOPES
    with pytest.raises(ValueError, match="unknown scope"):
        normalize_scopes(["read", "admin"])
    with pytest.raises(ValueError, match="at least one"):
        normalize_scopes(["", " "])

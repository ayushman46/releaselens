"""SecretBox: AES-256-GCM with HKDF-derived keys, row binding and key rotation."""

from __future__ import annotations

import pytest

from rl_db.secretbox import SecretBox, SecretBoxError, derive_key, slack_webhook_context

KEY = "k" * 40  # gitleaks:allow
OLD_KEY = "o" * 40  # gitleaks:allow
URL = "https://hooks.slack.com/services/T000/B000/XXXXXXXXXXXXXXXX"


def test_round_trip_and_ciphertext_hides_plaintext() -> None:
    box = SecretBox(KEY)
    ctx = slack_webhook_context("tenant-a", "rule-1")
    token = box.encrypt(URL, context=ctx)
    assert URL not in token
    assert "hooks.slack.com" not in token
    assert token.startswith(f"v1.{box.current_kid}.")
    assert box.decrypt(token, context=ctx) == URL


def test_fresh_nonce_per_encryption() -> None:
    box = SecretBox(KEY)
    assert box.encrypt(URL, context="c") != box.encrypt(URL, context="c")


def test_ciphertext_is_bound_to_its_row() -> None:
    box = SecretBox(KEY)
    token = box.encrypt(URL, context=slack_webhook_context("tenant-a", "rule-1"))
    with pytest.raises(SecretBoxError):
        box.decrypt(token, context=slack_webhook_context("tenant-b", "rule-1"))
    with pytest.raises(SecretBoxError):
        box.decrypt(token, context=slack_webhook_context("tenant-a", "rule-2"))


@pytest.mark.parametrize(
    "mangle",
    [
        lambda t: t[:-2] + ("AA" if not t.endswith("AA") else "BB"),
        lambda t: "v2" + t[2:],
        lambda t: t.split(".")[0],
        lambda t: "v1.deadbeef." + t.split(".", 2)[2],
        lambda t: "v1." + t.split(".")[1] + ".!!!",
        lambda t: "v1." + t.split(".")[1] + ".AAAA",
    ],
)
def test_tampered_or_malformed_tokens_fail(mangle: object) -> None:
    box = SecretBox(KEY)
    token = box.encrypt(URL, context="c")
    with pytest.raises(SecretBoxError):
        box.decrypt(mangle(token), context="c")  # type: ignore[operator]


def test_rotation_decrypts_old_and_flags_reencrypt() -> None:
    old = SecretBox(OLD_KEY).encrypt(URL, context="c")
    rotated = SecretBox(KEY, previous_keys=[OLD_KEY])
    assert rotated.decrypt(old, context="c") == URL
    assert rotated.needs_reencrypt(old)
    assert not rotated.needs_reencrypt(rotated.encrypt(URL, context="c"))
    with pytest.raises(SecretBoxError):
        SecretBox(KEY).decrypt(old, context="c")  # old key dropped: cannot decrypt


def test_short_master_key_is_rejected_and_labels_separate_keys() -> None:
    with pytest.raises(ValueError, match="at least"):
        SecretBox("short")
    assert derive_key(KEY, "a") != derive_key(KEY, "b")

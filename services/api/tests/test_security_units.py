"""Unit tests (no Docker): password policy and hashing, token shapes, client-IP resolution,
cookie flags and settings validation."""

from __future__ import annotations

import ipaddress
import uuid

import pytest

from rl_api import tokens
from rl_api.clientip import resolve_client_ip
from rl_api.passwords import PasswordHasher, password_problems
from rl_api.settings import Settings
from rl_api.testing import TEST_PASSWORD

DB = "postgresql+asyncpg://x:y@127.0.0.1:1/z"


@pytest.mark.parametrize(
    "password",
    [
        "short1!",
        "password1234",
        "P@ssw0rd2024!",
        "Welcome12345",
        "zzzzzzzzzzzzzzzz",
        "abababababababab",
        "1234567890123",
        "mnbvcxzmnbvcxz",
        "my-releaselens-pass",
        "x" * 300,
        "tab\tinside-password",
    ],
)
def test_policy_rejects(password: str) -> None:
    assert password_problems(password), password


@pytest.mark.parametrize(
    "password",
    [
        TEST_PASSWORD,
        "correct horse battery staple",
        "Tq8#vL2!pZ6@wR4m",
        "多語言のパスワードです十二",
    ],
)
def test_policy_accepts(password: str) -> None:
    assert password_problems(password) == []


def test_policy_rejects_the_email_local_part() -> None:
    assert password_problems("jane.doe-is-great-1", email="jane.doe@example.com")
    assert password_problems("Tq8#vL2!pZ6@wR4m", email="jane.doe@example.com") == []


async def test_argon2id_hash_verify_and_rehash_detection() -> None:
    weak = PasswordHasher(time_cost=1, memory_kib=1024, parallelism=1)
    strong = PasswordHasher(time_cost=2, memory_kib=2048, parallelism=1)
    encoded = await weak.hash(TEST_PASSWORD)
    assert encoded.startswith("$argon2id$v=19$m=1024,t=1,p=1$")
    assert await weak.verify(encoded, TEST_PASSWORD)
    assert not await weak.verify(encoded, TEST_PASSWORD + "x")
    assert not await weak.verify("not-a-hash", TEST_PASSWORD)
    assert not await weak.verify(None, TEST_PASSWORD)  # unknown account: same work, False
    assert not weak.needs_rehash(encoded)
    assert strong.needs_rehash(encoded)
    assert await strong.verify(encoded, TEST_PASSWORD)  # parameters travel in the hash


def test_tokens_shape_and_csrf_binding() -> None:
    token = tokens.new_token(tokens.REFRESH)
    assert tokens.well_formed(token, tokens.REFRESH)
    assert not tokens.well_formed(token, tokens.ACCESS)
    assert not tokens.well_formed(token + "x", tokens.REFRESH)
    assert not tokens.well_formed(None, tokens.REFRESH)
    assert len(tokens.hash_token(token)) == 64
    key = b"k" * 32
    a, b = uuid.uuid4(), uuid.uuid4()
    assert tokens.csrf_token_for(key, a) == tokens.csrf_token_for(key, a)
    assert tokens.csrf_token_for(key, a) != tokens.csrf_token_for(key, b)
    verifier, challenge = tokens.pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert len(challenge) == 43


TRUSTED = [ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("fd00::/8")]


@pytest.mark.parametrize(
    ("peer", "xff", "trusted", "expected"),
    [
        ("203.0.113.9", "1.2.3.4", TRUSTED, "203.0.113.9"),  # untrusted peer: header ignored
        ("203.0.113.9", None, [], "203.0.113.9"),
        ("10.0.0.2", "198.51.100.1", TRUSTED, "198.51.100.1"),
        ("10.0.0.2", "6.6.6.6, 198.51.100.1, 10.0.0.9", TRUSTED, "198.51.100.1"),
        ("10.0.0.2", "garbage, 10.0.0.9", TRUSTED, "10.0.0.2"),
        ("10.0.0.2", "10.0.0.5", TRUSTED, "10.0.0.5"),  # only proxies: the farthest one
        ("10.0.0.2", None, TRUSTED, "10.0.0.2"),
        ("fd00::1", "2001:db8::7", TRUSTED, "2001:db8::7"),
        ("testclient", None, [], None),
        (None, None, [], None),
    ],
)
def test_client_ip_resolution(
    peer: str | None, xff: str | None, trusted: list[object], expected: str | None
) -> None:
    assert resolve_client_ip(peer, xff, trusted) == expected  # type: ignore[arg-type]


def test_prod_requires_a_real_secret_key() -> None:
    with pytest.raises(ValueError, match="RL_SECRET_KEY"):
        Settings(env="prod", database_url=DB)
    with pytest.raises(ValueError, match="public development key"):
        Settings(
            env="prod",
            database_url=DB,
            secret_key="releaselens-dev-only-secret-key-not-for-production",
        )  # gitleaks:allow
    ok = Settings(env="prod", database_url=DB, secret_key="p" * 40)  # gitleaks:allow
    assert ok.master_secret() == "p" * 40
    assert Settings(env="dev", database_url=DB).master_secret()  # dev default exists


def test_settings_parse_csv_lists_and_reject_bad_cidrs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RL_TRUSTED_PROXIES", "10.0.0.0/8, 192.168.0.0/16")
    assert Settings(database_url=DB).trusted_proxies == ["10.0.0.0/8", "192.168.0.0/16"]
    monkeypatch.setenv("RL_TRUSTED_PROXIES", "not-a-cidr")
    with pytest.raises(ValueError):
        Settings(database_url=DB)


def test_secure_cookie_only_dropped_for_dev_over_http_localhost() -> None:
    from starlette.requests import Request

    from rl_api.sessions import cookie_secure

    def request(url: str) -> Request:
        scheme, rest = url.split("://", 1)
        host, _, path = rest.partition("/")
        hostname, _, port = host.partition(":")
        return Request(
            {
                "type": "http",
                "scheme": scheme,
                "server": (hostname, int(port or (443 if scheme == "https" else 80))),
                "path": "/" + path,
                "headers": [(b"host", host.encode())],
                "query_string": b"",
            }
        )

    dev = Settings(env="dev", database_url=DB)
    test = Settings(env="test", database_url=DB)
    assert cookie_secure(request("http://localhost:28000/x"), dev) is False
    assert cookie_secure(request("http://127.0.0.1:28000/x"), dev) is False
    assert cookie_secure(request("https://localhost:28000/x"), dev) is True
    assert cookie_secure(request("http://api.example.com/x"), dev) is True
    assert cookie_secure(request("http://localhost:28000/x"), test) is True

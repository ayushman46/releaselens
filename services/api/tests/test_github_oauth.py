"""Sign in with GitHub: state + PKCE, browser binding, account linking rules (GitHub is an
``httpx.MockTransport``; nothing leaves the process)."""

from __future__ import annotations

import base64
import hashlib
import uuid
from collections.abc import AsyncIterator
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

from rl_api.testing import TEST_PASSWORD, FakeGitHub, Harness, saas_settings, unique_email
from rl_db.models import User

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def db_url(pg_server_url: str) -> str:
    from rl_db.testing import create_migrated_database

    return create_migrated_database(pg_server_url, prefix="rl_oauth")


def _settings(db_url: str, **kw: object) -> object:
    return saas_settings(
        db_url,
        github_oauth_client_id="Iv1.testclient",
        github_oauth_client_secret="test-client-secret",  # gitleaks:allow
        **kw,
    )


@pytest.fixture
async def h(db_url: str) -> AsyncIterator[Harness]:
    harness = Harness(_settings(db_url))  # type: ignore[arg-type]
    async with harness.running():
        yield harness


async def _start(h: Harness) -> tuple[object, str, dict[str, list[str]]]:
    browser = h.client()
    resp = await browser.get("/api/v1/auth/github/authorize")
    assert resp.status_code == 302, resp.text
    location = resp.headers["location"]
    query = parse_qs(urlsplit(location).query)
    return browser, location, query


async def test_not_configured_is_503(db_url: str) -> None:
    harness = Harness(saas_settings(db_url))
    async with harness.running():
        client = harness.client()
        for path in (
            "/api/v1/auth/github/authorize",
            "/api/v1/auth/github/callback?code=x&state=y",
        ):
            resp = await client.get(path)
            assert resp.status_code == 503
            assert resp.json()["error"]["code"] == "github_oauth_not_configured"


async def test_authorize_uses_state_and_pkce_s256(h: Harness) -> None:
    _, location, query = await _start(h)
    assert location.startswith("https://github.com/login/oauth/authorize?")
    assert query["client_id"] == ["Iv1.testclient"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == ["http://localhost:28000/api/v1/auth/github/callback"]
    assert query["state"][0].startswith("rls_")
    assert "user:email" in query["scope"][0]


async def test_callback_creates_a_verified_account_and_signs_in(h: Harness) -> None:
    h.github.emails = [
        {"email": "secondary@example.com", "primary": False, "verified": True},
        {"email": f"octo-{uuid.uuid4().hex[:6]}@example.com", "primary": True, "verified": True},
    ]
    h.github.user_id = 100_000 + uuid.uuid4().int % 1_000_000
    browser, _, query = await _start(h)
    state = query["state"][0]
    resp = await browser.get(f"/api/v1/auth/github/callback?code=abc&state={state}")
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://app.releaselens.test/app"
    me = await browser.get("/api/v1/me")
    assert me.status_code == 200
    user = me.json()["user"]
    assert user["email"] == h.github.emails[1]["email"]
    assert user["github_linked"] is True
    assert user["has_password"] is False

    # The PKCE verifier sent to GitHub hashes to the challenge sent to the browser.
    token_request = next(r for r in h.github.requests if r.url.path == "/login/oauth/access_token")
    form = parse_qs(token_request.content.decode())
    verifier = form["code_verifier"][0]
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert digest.decode() == query["code_challenge"][0]
    assert form["client_secret"] == ["test-client-secret"]  # gitleaks:allow

    # The state is single use.
    replay = await browser.get(f"/api/v1/auth/github/callback?code=abc&state={state}")
    assert replay.headers["location"].endswith(
        "/login?error=oauth_state_mismatch"
    ) or replay.headers["location"].endswith("/login?error=oauth_state_expired")


async def test_callback_from_another_browser_is_refused(h: Harness) -> None:
    _, _, query = await _start(h)
    attacker_link = f"/api/v1/auth/github/callback?code=abc&state={query['state'][0]}"
    victim = h.client()  # never started the flow: no state cookie
    resp = await victim.get(attacker_link)
    assert resp.status_code == 302
    assert resp.headers["location"].endswith("/login?error=oauth_state_mismatch")
    assert (await victim.get("/api/v1/me")).status_code == 401


async def test_links_existing_verified_account_by_verified_primary_email(h: Harness) -> None:
    email = unique_email("linked")
    await h.signup(email)
    h.github.emails = [{"email": email, "primary": True, "verified": True}]
    h.github.user_id = 200_000 + uuid.uuid4().int % 1_000_000
    browser, _, query = await _start(h)
    resp = await browser.get(f"/api/v1/auth/github/callback?code=c&state={query['state'][0]}")
    assert resp.status_code == 302
    me = (await browser.get("/api/v1/me")).json()["user"]
    assert me["email"] == email
    assert me["github_linked"] is True
    assert me["has_password"] is True  # both proofs of the address agree: keep the password
    await h.login(email, TEST_PASSWORD)


async def test_unverified_pre_registered_password_is_dropped_on_github_link(h: Harness) -> None:
    """Account pre-hijacking: someone registered the victim's address but never verified
    it. When the real owner arrives through GitHub, the squatter's password must die."""
    email = unique_email("victim")
    squatter = h.client()
    resp = await squatter.post(
        "/api/v1/auth/signup", json={"email": email, "password": TEST_PASSWORD}
    )
    assert resp.status_code == 202
    h.github.emails = [{"email": email, "primary": True, "verified": True}]
    h.github.user_id = 300_000 + uuid.uuid4().int % 1_000_000
    browser, _, query = await _start(h)
    await browser.get(f"/api/v1/auth/github/callback?code=c&state={query['state'][0]}")
    async with h.app.state.db_sessions() as s:
        row = (await s.execute(select(User).where(User.email == email))).scalar_one()
    assert row.password_hash is None
    assert row.email_verified_at is not None
    bad = await squatter.post(
        "/api/v1/auth/login", json={"email": email, "password": TEST_PASSWORD}
    )
    assert bad.status_code == 401


@pytest.mark.parametrize(
    ("setup", "error"),
    [
        (lambda gh: setattr(gh, "token_error", "bad_verification_code"), "github_code_rejected"),
        (
            lambda gh: setattr(
                gh, "emails", [{"email": "x@example.com", "primary": True, "verified": False}]
            ),
            "github_no_verified_email",
        ),
    ],
)
async def test_github_failures_redirect_with_a_code(h: Harness, setup: object, error: str) -> None:
    gh: FakeGitHub = h.github
    setup(gh)  # type: ignore[operator]
    browser, _, query = await _start(h)
    resp = await browser.get(f"/api/v1/auth/github/callback?code=c&state={query['state'][0]}")
    assert resp.status_code == 302
    assert resp.headers["location"] == f"https://app.releaselens.test/login?error={error}"
    assert (await browser.get("/api/v1/me")).status_code == 401


async def test_user_denied_on_github(h: Harness) -> None:
    browser, _, _ = await _start(h)
    resp = await browser.get("/api/v1/auth/github/callback?error=access_denied")
    assert resp.headers["location"].endswith("/login?error=github_denied")

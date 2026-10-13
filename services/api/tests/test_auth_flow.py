"""Sign-up, verification, login, sessions (refresh rotation, reuse detection, logout),
CSRF, password reset and rehash-on-login, against a real Postgres (M10)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from rl_api.testing import TEST_PASSWORD, Harness, cookie_header, saas_settings, unique_email
from rl_db.models import AuditLogEntry, AuthSession, User

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def db_url(pg_server_url: str) -> str:
    from rl_db.testing import create_migrated_database

    return create_migrated_database(pg_server_url, prefix="rl_auth")


@pytest.fixture
async def h(db_url: str) -> AsyncIterator[Harness]:
    harness = Harness(saas_settings(db_url))
    async with harness.running():
        yield harness


async def _user_row(h: Harness, email: str) -> User:
    async with h.app.state.db_sessions() as s:
        row = (await s.execute(select(User).where(User.email == email))).scalar_one()
    return row


# ----------------------------------------------------------------------------- signup & verify
async def test_signup_verify_login_and_cookie_flags(h: Harness) -> None:
    email = unique_email("alice")
    client = h.client()
    resp = await client.post(
        "/api/v1/auth/signup", json={"email": email.upper(), "password": TEST_PASSWORD}
    )
    assert resp.status_code == 202
    assert resp.json()["status"] == "accepted"

    # Not verified yet: the right password still cannot sign in.
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": TEST_PASSWORD})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "email_not_verified"

    mail = h.mailer.last("verify_email", email)
    assert "https://app.releaselens.test/verify-email#token=rlv_" in mail.text
    token = h.token_from_mail("verify_email", email)
    assert (
        await client.post("/api/v1/auth/verify-email", json={"token": token})
    ).status_code == 200
    again = await client.post("/api/v1/auth/verify-email", json={"token": token})
    assert again.status_code == 400  # single use
    assert again.json()["error"]["code"] == "invalid_token"

    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user"]["email"] == email
    assert body["user"]["email_verified"] is True
    assert body["csrf_token"]
    set_cookies = resp.headers.get_list("set-cookie")
    access = next(c for c in set_cookies if c.startswith("rl_access="))
    refresh = next(c for c in set_cookies if c.startswith("rl_refresh="))
    csrf = next(c for c in set_cookies if c.startswith("rl_csrf="))
    for cookie in (access, refresh):
        assert "HttpOnly" in cookie
        assert "Secure" in cookie
        assert "SameSite=lax" in cookie
    assert "HttpOnly" not in csrf  # the web app must read it
    assert "Path=/api/v1/auth" in refresh
    assert resp.headers["cache-control"] == "no-store"
    assert csrf.split(";")[0] == f"rl_csrf={body['csrf_token']}"

    me = await client.get("/api/v1/me")
    assert me.status_code == 200
    assert me.json()["user"]["email"] == email
    assert me.json()["memberships"] == []


async def test_signup_rejects_weak_passwords_and_bad_emails(h: Harness) -> None:
    client = h.client()
    for password, reason in [
        ("short", "at least 12"),
        ("Password123!", "common word"),
        ("aaaaaaaaaaaaaaaa", "too few"),
        ("abcdefghijklmnop", "sequence"),
        ("0987654321098", "sequence"),
        ("qwertyuiop1234567890", "common word"),
    ]:
        resp = await client.post(
            "/api/v1/auth/signup", json={"email": unique_email(), "password": password}
        )
        assert resp.status_code == 422, password
        assert resp.json()["error"]["code"] == "weak_password"
        assert any(reason in r for r in resp.json()["error"]["details"]["reasons"]), password
    email = unique_email("named")
    resp = await client.post(
        "/api/v1/auth/signup", json={"email": email, "password": email.split("@")[0] + "-x9!Q"}
    )
    assert resp.status_code == 422  # contains the address
    for bad in ["no-at-sign", "a@b", "a b@example.com", "x@exa..mple.com"]:
        resp = await client.post(
            "/api/v1/auth/signup", json={"email": bad, "password": TEST_PASSWORD}
        )
        assert resp.status_code == 422, bad


async def test_signup_for_existing_address_is_indistinguishable_and_harmless(h: Harness) -> None:
    email = unique_email("taken")
    await h.signup(email)
    client = h.client()
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "Another-Strong-Pass-88"},  # gitleaks:allow
    )
    fresh = await client.post(
        "/api/v1/auth/signup", json={"email": unique_email(), "password": TEST_PASSWORD}
    )
    assert resp.status_code == fresh.status_code == 202
    assert resp.json() == fresh.json()
    assert "/reset-password#token=rlp_" in h.mailer.last("account_exists", email).text
    # The existing password was not replaced.
    await h.login(email)
    bad = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "Another-Strong-Pass-88"},  # gitleaks:allow
    )
    assert bad.status_code == 401


# ----------------------------------------------------------------------------- login failures
async def test_login_failures_do_not_enumerate_accounts_and_are_audited(h: Harness) -> None:
    email = unique_email("bob")
    await h.signup(email)
    client = h.client()
    wrong_password = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "Not-The-Password-77"},  # gitleaks:allow
    )
    no_account = await client.post(
        "/api/v1/auth/login", json={"email": unique_email("ghost"), "password": TEST_PASSWORD}
    )
    assert wrong_password.status_code == no_account.status_code == 401
    w, n = wrong_password.json()["error"], no_account.json()["error"]
    assert (
        (w["code"], w["message"])
        == (n["code"], n["message"])
        == (
            "invalid_credentials",
            "invalid e-mail or password",
        )
    )
    async with h.app.state.db_sessions() as s:
        failures = (
            (
                await s.execute(
                    select(AuditLogEntry).where(AuditLogEntry.action == "auth.login_failed")
                )
            )
            .scalars()
            .all()
        )
    mine = [f for f in failures if f.details.get("reason") == "invalid_credentials"]
    assert len(mine) >= 2
    assert all(f.tenant_id is None and f.actor_user_id is None for f in mine)
    assert all(email not in str(f.details) for f in mine)  # fingerprint only
    assert all(str(f.ip) == "127.0.0.1" for f in mine)


# ----------------------------------------------------------------------------- sessions
async def test_refresh_rotates_and_reuse_revokes_the_whole_family(h: Harness) -> None:
    email = unique_email("carol")
    browser = await h.signup(email)
    old_access = browser.cookies.get("rl_access")
    old_refresh = browser.cookies.get("rl_refresh", path="/api/v1/auth")

    resp = await browser.post("/api/v1/auth/refresh")
    assert resp.status_code == 200, resp.text
    new_access = browser.cookies.get("rl_access")
    assert new_access != old_access
    assert browser.cookies.get("rl_refresh", path="/api/v1/auth") != old_refresh
    assert (await browser.get("/api/v1/me")).status_code == 200

    # The old access token died with the rotation.
    stale = await h.client().get("/api/v1/me", headers=cookie_header(rl_access=old_access))
    assert stale.status_code == 401

    # A thief replays the old refresh token: the whole session dies, the new tokens too.
    replay = await h.client().post(
        "/api/v1/auth/refresh",
        headers={
            **cookie_header(rl_refresh=old_refresh, rl_csrf=browser.csrf),
            "X-CSRF-Token": browser.csrf or "",
        },
    )
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "refresh_token_reused"
    assert (await browser.get("/api/v1/me")).status_code == 401
    assert (await browser.post("/api/v1/auth/refresh")).status_code == 401
    async with h.app.state.db_sessions() as s:
        reasons = (
            (
                await s.execute(
                    select(AuthSession.revoked_reason).where(AuthSession.user_id == browser.user_id)
                )
            )
            .scalars()
            .all()
        )
    assert "refresh_token_reuse" in reasons


async def test_refresh_needs_csrf_and_a_failed_check_does_not_burn_the_token(h: Harness) -> None:
    browser = await h.signup(unique_email("dave"))
    resp = await browser.post("/api/v1/auth/refresh", headers={"X-CSRF-Token": "wrong"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "csrf_failed"
    assert (await browser.post("/api/v1/auth/refresh")).status_code == 200


async def test_csrf_is_required_for_cookie_authenticated_mutations(h: Harness) -> None:
    browser = await h.signup(unique_email("erin"))
    body = {"name": "Acme", "slug": f"acme-{uuid.uuid4().hex[:8]}"}
    no_header = await browser.post("/api/v1/orgs", json=body, headers={"X-CSRF-Token": ""})
    assert no_header.status_code == 403
    assert no_header.json()["error"]["code"] == "csrf_failed"
    forged = await browser.post("/api/v1/orgs", json=body, headers={"X-CSRF-Token": "x" * 43})
    assert forged.status_code == 403
    # Header and cookie agree, but not with the session's own token (cookie tossing).
    tossed = await h.client().post(
        "/api/v1/orgs",
        json=body,
        headers={
            **cookie_header(rl_access=browser.cookies.get("rl_access"), rl_csrf="y" * 43),
            "X-CSRF-Token": "y" * 43,
        },
    )
    assert tossed.status_code == 403
    assert (await browser.post("/api/v1/orgs", json=body)).status_code == 201
    # Safe methods need no token.
    assert (await browser.get("/api/v1/orgs", headers={"X-CSRF-Token": ""})).status_code == 200


async def test_logout_revokes_and_clears_cookies(h: Harness) -> None:
    browser = await h.signup(unique_email("frank"))
    access = browser.cookies.get("rl_access")
    assert (
        await browser.post("/api/v1/auth/logout", headers={"X-CSRF-Token": ""})
    ).status_code == 403
    resp = await browser.post("/api/v1/auth/logout")
    assert resp.status_code == 204
    assert any(
        c.startswith('rl_access=""') or "Max-Age=0" in c
        for c in resp.headers.get_list("set-cookie")
    )
    replay = await h.client().get("/api/v1/me", headers=cookie_header(rl_access=access))
    assert replay.status_code == 401


async def test_expired_access_token_is_rejected(h: Harness) -> None:
    browser = await h.signup(unique_email("gail"))
    async with h.app.state.db_sessions.begin() as s:
        await s.execute(
            update(AuthSession)
            .where(AuthSession.user_id == browser.user_id)
            .values(access_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert (await browser.get("/api/v1/me")).status_code == 401
    assert (await browser.post("/api/v1/auth/refresh")).status_code == 200  # refresh still works
    assert (await browser.get("/api/v1/me")).status_code == 200


async def test_unauthenticated_requests_get_401(h: Harness) -> None:
    anon = h.client()
    assert (await anon.get("/api/v1/me")).status_code == 401
    for forged in ("rla_" + "A" * 43, "garbage"):
        resp = await anon.get("/api/v1/me", headers=cookie_header(rl_access=forged))
        assert resp.status_code == 401


# ----------------------------------------------------------------------------- password reset
async def test_password_reset_flow(h: Harness) -> None:
    email = unique_email("hank")
    first_browser = await h.signup(email)
    client = h.client()
    unknown = await client.post(
        "/api/v1/auth/password-reset/request", json={"email": unique_email("nobody")}
    )
    known = await client.post("/api/v1/auth/password-reset/request", json={"email": email})
    assert unknown.status_code == known.status_code == 202
    assert unknown.json() == known.json()
    token = h.token_from_mail("reset_password", email)

    weak = await client.post(
        "/api/v1/auth/password-reset/confirm", json={"token": token, "new_password": "password1"}
    )
    assert weak.status_code == 422  # and the token survives the failed attempt
    new_password = "Velvet-Harbor-Quartz-19"  # gitleaks:allow
    ok = await client.post(
        "/api/v1/auth/password-reset/confirm", json={"token": token, "new_password": new_password}
    )
    assert ok.status_code == 200, ok.text
    reused = await client.post(
        "/api/v1/auth/password-reset/confirm", json={"token": token, "new_password": new_password}
    )
    assert reused.status_code == 400
    # Every existing session was signed out; the old password no longer works.
    assert (await first_browser.get("/api/v1/me")).status_code == 401
    old = await client.post("/api/v1/auth/login", json={"email": email, "password": TEST_PASSWORD})
    assert old.status_code == 401
    await h.login(email, new_password)


async def test_reset_tokens_expire(h: Harness) -> None:
    from rl_db.models import UserToken

    email = unique_email("ivy")
    await h.signup(email)
    client = h.client()
    await client.post("/api/v1/auth/password-reset/request", json={"email": email})
    token = h.token_from_mail("reset_password", email)
    async with h.app.state.db_sessions.begin() as s:
        await s.execute(
            update(UserToken)
            .where(UserToken.purpose == "reset_password")
            .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
    resp = await client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": token, "new_password": "Velvet-Harbor-Quartz-19"},  # gitleaks:allow
    )
    assert resp.status_code == 400


async def test_login_rehashes_when_argon2_parameters_change(h: Harness, db_url: str) -> None:
    email = unique_email("judy")
    await h.signup(email)
    before = (await _user_row(h, email)).password_hash
    assert before is not None
    assert ",t=1," in before

    stronger = Harness(saas_settings(db_url, argon2_time_cost=2))
    async with stronger.running():
        await stronger.login(email)
    after = (await _user_row(h, email)).password_hash
    assert after is not None
    assert after != before
    assert after.startswith("$argon2id$")
    assert ",t=2," in after
    await h.login(email)  # still verifies with the old process's parameters

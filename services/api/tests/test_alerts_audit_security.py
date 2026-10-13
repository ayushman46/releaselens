"""Alert rules (encrypted Slack webhooks, plan gate, URL rules), the audit log API and CSV
export, rate limits, security headers, body limits and CORS (M10)."""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select, update

from rl_api.testing import Harness, UserClient, saas_settings, unique_email
from rl_db.models import AlertRule, Organization, Plan
from rl_db.secretbox import SecretBox, slack_webhook_context

pytestmark = pytest.mark.integration

SLACK_URL = "https://hooks.slack.com/services/T0001/B0001/abcdefghijklmnopqrstwxyz"


@pytest.fixture(scope="module")
def db_url(pg_server_url: str) -> str:
    from rl_db.testing import create_migrated_database

    return create_migrated_database(pg_server_url, prefix="rl_alerts")


@pytest.fixture
async def h(db_url: str) -> AsyncIterator[Harness]:
    harness = Harness(saas_settings(db_url))
    async with harness.running():
        yield harness


async def _project(h: Harness, *, team: bool) -> tuple[UserClient, uuid.UUID, str]:
    owner = await h.signup(unique_email("alerts"))
    org_id = await h.create_org(owner)
    if team:
        async with h.app.state.db_sessions.begin() as s:
            await s.execute(
                update(Organization).where(Organization.id == org_id).values(plan=Plan.TEAM)
            )
    resp = await owner.post(f"/api/v1/orgs/{org_id}/projects", json={"name": "P", "slug": "p"})
    return owner, org_id, resp.json()["id"]


# ----------------------------------------------------------------------------- alert rules
async def test_slack_webhook_is_encrypted_at_rest_and_never_returned(h: Harness) -> None:
    owner, org_id, pid = await _project(h, team=True)
    base = f"/api/v1/orgs/{org_id}/projects/{pid}/alert-rules"
    resp = await owner.post(
        base,
        json={
            "name": "on-call",
            "slack_webhook_url": SLACK_URL,
            "email_recipients": ["B@example.com", "b@example.com", "a@example.com"],
            "regression_min_band": "CRITICAL",
        },
    )
    assert resp.status_code == 201, resp.text
    rule = resp.json()
    assert rule["slack"] == {"configured": True, "hint": "https://hooks.slack.com/…wxyz"}
    assert rule["email_recipients"] == ["a@example.com", "b@example.com"]
    for payload in (
        rule,
        (await owner.get(base)).json(),
        (await owner.get(f"{base}/{rule['id']}")).json(),
    ):
        assert SLACK_URL not in str(payload)
        assert "T0001/B0001" not in str(payload)

    async with h.app.state.db_sessions() as s:
        row = await s.get(AlertRule, uuid.UUID(rule["id"]))
    assert row is not None
    assert row.slack_webhook_ciphertext is not None
    assert "hooks.slack.com" not in row.slack_webhook_ciphertext
    box: SecretBox = h.app.state.secret_box
    context = slack_webhook_context(org_id, row.id)
    assert box.decrypt(row.slack_webhook_ciphertext, context=context) == SLACK_URL

    audit = (
        await owner.get(f"/api/v1/orgs/{org_id}/audit-log", params={"target_type": "alert_rule"})
    ).json()
    assert audit["items"][0]["metadata"]["slack_webhook"] == "set"
    assert SLACK_URL not in str(audit)

    removed = await owner.patch(f"{base}/{rule['id']}", json={"slack_webhook_url": None})
    assert removed.json()["slack"] == {"configured": False, "hint": None}
    unchanged = await owner.patch(f"{base}/{rule['id']}", json={"enabled": False})
    assert unchanged.json()["email_recipients"] == ["a@example.com", "b@example.com"]
    assert unchanged.json()["enabled"] is False
    assert (await owner.delete(f"{base}/{rule['id']}")).status_code == 204


async def test_slack_alerts_need_the_team_plan(h: Harness) -> None:
    owner, org_id, pid = await _project(h, team=False)
    base = f"/api/v1/orgs/{org_id}/projects/{pid}/alert-rules"
    resp = await owner.post(base, json={"name": "x", "slack_webhook_url": SLACK_URL})
    assert resp.status_code == 402
    err = resp.json()["error"]
    assert (err["code"], err["details"]["limit"], err["details"]["upgrade"]["plan"]) == (
        "plan_limit_reached",
        "slack_alerts",
        "team",
    )
    # E-mail alerts are fine on Free.
    assert (
        await owner.post(base, json={"name": "x", "email_recipients": ["a@example.com"]})
    ).status_code == 201


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data",
        "https://hooks.slack.com.evil.example/services/x",
        "https://user:pass@hooks.slack.com/services/x",
        "https://hooks.slack.com:8443/services/x",
        "https://hooks.slack.com/other/x",
        "ftp://hooks.slack.com/services/x",
        "https://hooks.slack.com/services/x y",
    ],
)
async def test_webhook_urls_outside_slack_are_refused_in_prod_rules(url: str) -> None:
    from rl_core.alerting import validate_slack_webhook_url

    with pytest.raises(ValueError):
        validate_slack_webhook_url(url, allow_any_host=False)
    assert validate_slack_webhook_url(SLACK_URL, allow_any_host=False) == SLACK_URL


async def test_invalid_webhook_url_is_422(h: Harness) -> None:
    owner, org_id, pid = await _project(h, team=True)
    resp = await owner.post(
        f"/api/v1/orgs/{org_id}/projects/{pid}/alert-rules",
        json={"name": "x", "slack_webhook_url": "ftp://example.com/hook"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_webhook_url"


# ----------------------------------------------------------------------------- audit log API
async def test_audit_filters_pagination_and_csv_export_is_formula_safe(h: Harness) -> None:
    owner, org_id, _ = await _project(h, team=False)
    # A key name that would execute as a spreadsheet formula, plus quotes and commas.
    nasty = '=HYPERLINK("http://evil.example","click"), "quoted"'
    resp = await owner.post(
        f"/api/v1/orgs/{org_id}/api-keys", json={"name": nasty, "scopes": ["read"]}
    )
    assert resp.status_code == 201
    for i in range(3):
        await owner.patch(f"/api/v1/orgs/{org_id}", json={"name": f"Renamed {i}"})

    base = f"/api/v1/orgs/{org_id}/audit-log"
    page1 = (await owner.get(base, params={"limit": 2})).json()
    assert len(page1["items"]) == 2
    assert page1["next_cursor"]
    page2 = (await owner.get(base, params={"limit": 2, "cursor": page1["next_cursor"]})).json()
    assert {e["id"] for e in page1["items"]}.isdisjoint({e["id"] for e in page2["items"]})
    renames = (await owner.get(base, params={"action": "org.settings_changed"})).json()["items"]
    assert [e["metadata"]["to"] for e in renames] == ["Renamed 2", "Renamed 1", "Renamed 0"]
    assert renames[0]["ip"] == "127.0.0.1"
    assert (await owner.get(base, params={"cursor": "%%%"})).status_code == 400

    export = await owner.get(f"{base}/export.csv")
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/csv")
    assert "attachment;" in export.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(export.text)))
    header, body = rows[0], rows[1:]
    assert header[:2] == ["created_at", "action"]
    meta = [
        r[header.index("metadata")] for r in body if r[header.index("action")] == "api_key.created"
    ]
    assert len(meta) == 1
    assert '"name":"=HYPERLINK' in meta[0]  # JSON text, inside a quoted cell
    # Every cell that a spreadsheet would evaluate is neutralised with a leading quote.
    formula_cells = [c for r in body for c in r if c[:1] in ("=", "+", "-", "@")]
    assert formula_cells == []
    assert export.text.count('"') % 2 == 0  # balanced quoting

    filtered = await owner.get(f"{base}/export.csv", params={"action": "api_key.created"})
    assert len(list(csv.reader(io.StringIO(filtered.text)))) == 2  # header + one row


def test_safe_cell_neutralises_formula_prefixes() -> None:
    from rl_api.routers.audit import safe_cell

    for raw in ("=1+1", "+1", "-1", "@SUM(A1)", "\tcmd", "\rcmd"):
        assert safe_cell(raw).startswith("'")
    assert safe_cell("plain") == "plain"
    assert safe_cell(None) == ""


# ----------------------------------------------------------------------------- rate limits
async def test_login_rate_limit_trips_with_retry_after(db_url: str) -> None:
    harness = Harness(
        saas_settings(db_url, rate_limit_auth_burst=3, rate_limit_auth_per_minute=0.5)
    )
    async with harness.running():
        client = harness.client(peer="198.51.100.7")
        codes = []
        for _ in range(4):
            resp = await client.post(
                "/api/v1/auth/login", json={"email": unique_email(), "password": "Whatever-Pass-12"}
            )
            codes.append(resp.status_code)
        assert codes == [401, 401, 401, 429]
        assert resp.json()["error"]["code"] == "rate_limited"
        assert int(resp.headers["retry-after"]) >= 1
        # Another client address has its own bucket.
        other = harness.client(peer="198.51.100.8")
        resp = await other.post(
            "/api/v1/auth/password-reset/request", json={"email": unique_email()}
        )
        assert resp.status_code == 202


async def test_forwarded_for_is_ignored_unless_the_peer_is_a_trusted_proxy(db_url: str) -> None:
    harness = Harness(
        saas_settings(
            db_url,
            rate_limit_auth_burst=2,
            rate_limit_auth_per_minute=0.5,
            trusted_proxies=["10.0.0.0/8"],
        )
    )
    async with harness.running():
        direct = harness.client(peer="203.0.113.50")
        statuses = [
            (
                await direct.post(
                    "/api/v1/auth/password-reset/request",
                    json={"email": unique_email()},
                    headers={"X-Forwarded-For": f"192.0.2.{i}"},  # spoofed: not from a proxy
                )
            ).status_code
            for i in range(3)
        ]
        assert statuses == [202, 202, 429]
        proxy = harness.client(peer="10.1.2.3")
        statuses = [
            (
                await proxy.post(
                    "/api/v1/auth/password-reset/request",
                    json={"email": unique_email()},
                    headers={"X-Forwarded-For": f"192.0.2.{i}, 10.9.9.9"},
                )
            ).status_code
            for i in range(3)
        ]
        assert statuses == [202, 202, 202]  # three distinct real clients behind the proxy


async def test_ingest_is_rate_limited_per_api_key(db_url: str) -> None:
    harness = Harness(
        saas_settings(db_url, rate_limit_ingest_burst=2, rate_limit_ingest_per_minute=0.5)
    )
    async with harness.running():
        owner = await harness.signup(unique_email("ingest"))
        org_id = await harness.create_org(owner)
        key = (
            await owner.post(
                f"/api/v1/orgs/{org_id}/api-keys",
                json={"name": "cd", "scopes": ["deployments:write"]},
            )
        ).json()["key"]
        client = harness.client()
        body = {
            "service": "payment",
            "environment": "production",
            "version": "v2",
            "commit_sha": "abcdef1",
            "deployed_at": "2026-10-13T00:00:00Z",
        }
        statuses = [
            (
                await client.post(
                    "/api/v1/deployments", json=body, headers={"x-releaselens-key": key}
                )
            ).status_code
            for _ in range(3)
        ]
        # No broker in this test: accepted requests end in 503, the third is throttled.
        assert statuses == [503, 503, 429]


async def test_otlp_ingest_has_its_own_per_key_limit(db_url: str) -> None:
    harness = Harness(
        saas_settings(
            db_url,
            rate_limit_telemetry_burst=2,
            rate_limit_telemetry_per_minute=0.5,
            rate_limit_ingest_burst=1,
            rate_limit_ingest_per_minute=0.5,
        )
    )
    async with harness.running():
        owner = await harness.signup(unique_email("otlp-limit"))
        org_id = await harness.create_org(owner)
        key = (
            await owner.post(
                f"/api/v1/orgs/{org_id}/api-keys",
                json={"name": "collector", "scopes": ["telemetry:write", "deployments:write"]},
            )
        ).json()["key"]
        client = harness.client()
        headers = {"x-releaselens-key": key, "content-type": "application/json"}
        statuses = [
            (
                await client.post("/v1/traces", content=b'{"resourceSpans": []}', headers=headers)
            ).status_code
            for _ in range(3)
        ]
        assert statuses[:2] != [429, 429]
        assert statuses[2] == 429
        # A separate bucket: telemetry traffic does not use up the deployment-event budget.
        deploy = await client.post(
            "/api/v1/deployments",
            json={
                "service": "payment",
                "environment": "production",
                "version": "v2",
                "commit_sha": "abcdef1",
                "deployed_at": "2026-10-13T00:00:00Z",
            },
            headers={"x-releaselens-key": key},
        )
        assert deploy.status_code != 429


# ----------------------------------------------------------------------------- headers & limits
async def test_security_headers_on_every_response(h: Harness) -> None:
    for path in ("/healthz", "/api/v1/plans", "/api/v1/does-not-exist"):
        resp = await h.client().get(path)
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["x-frame-options"] == "DENY"
        assert resp.headers["referrer-policy"] == "no-referrer"
        assert "default-src 'none'" in resp.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]
        assert resp.headers["strict-transport-security"].startswith("max-age=")  # env=test


async def test_no_hsts_in_dev(db_url: str) -> None:
    harness = Harness(saas_settings(db_url, env="dev"))
    async with harness.running():
        resp = await harness.client().get("/healthz")
        assert "strict-transport-security" not in resp.headers


async def test_oversized_bodies_are_413(db_url: str) -> None:
    harness = Harness(saas_settings(db_url, max_body_bytes=2048))
    async with harness.running():
        client = harness.client()
        big = {"email": "a@example.com", "password": "x" * 5000}
        resp = await client.post("/api/v1/auth/login", json=big)
        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == "payload_too_large"

        async def chunks() -> AsyncIterator[bytes]:  # no Content-Length: streamed
            yield b'{"email": "a@example.com", "password": "'
            for _ in range(10):
                yield b"y" * 512
            yield b'"}'

        streamed = await client.post(
            "/api/v1/auth/login", content=chunks(), headers={"Content-Type": "application/json"}
        )
        assert streamed.status_code == 413


async def test_cors_allows_credentials_only_for_configured_origins(h: Harness) -> None:
    allowed = await h.client().options(
        "/api/v1/auth/login",
        headers={
            "Origin": "http://localhost:23000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-csrf-token",
        },
    )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:23000"
    assert allowed.headers["access-control-allow-credentials"] == "true"
    denied = await h.client().options(
        "/api/v1/auth/login",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in denied.headers


def test_wildcard_cors_is_refused() -> None:
    with pytest.raises(ValueError, match="explicitly"):
        saas_settings("postgresql+asyncpg://x:y@127.0.0.1:1/z", cors_origins=["*"])


async def test_audit_rows_record_the_trusted_client_ip(db_url: str) -> None:
    from rl_db.models import AuditLogEntry

    harness = Harness(saas_settings(db_url, trusted_proxies=["10.0.0.0/8"]))
    async with harness.running():
        client = harness.client(peer="10.0.0.1")
        await client.post(
            "/api/v1/auth/login",
            json={"email": unique_email("ip"), "password": "Whatever-Pass-12"},
            headers={"X-Forwarded-For": "198.51.100.99", "User-Agent": "audit-test/1.0"},
        )
        async with harness.app.state.db_sessions() as s:
            row = (
                (
                    await s.execute(
                        select(AuditLogEntry)
                        .where(AuditLogEntry.user_agent == "audit-test/1.0")
                        .order_by(AuditLogEntry.created_at.desc())
                    )
                )
                .scalars()
                .first()
            )
        assert row is not None
        assert str(row.ip) == "198.51.100.99"

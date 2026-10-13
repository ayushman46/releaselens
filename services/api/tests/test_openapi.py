"""The OpenAPI document (source of the web app's typed client) renders and is accurate."""

from __future__ import annotations

from fastapi.testclient import TestClient

from rl_api.app import create_app
from rl_api.settings import Settings


def test_openapi_describes_the_deployment_endpoints() -> None:
    app = create_app(
        Settings(
            env="test",
            database_url="postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none",
            kafka_bootstrap="127.0.0.1:1",
            otel_enabled=False,
        )
    )
    with TestClient(app) as client:
        spec = client.get("/openapi.json").json()
    paths = spec["paths"]
    assert set(paths["/api/v1/deployments"]) == {"get", "post"}
    assert "get" in paths["/api/v1/deployments/{deployment_id}"]
    post = paths["/api/v1/deployments"]["post"]
    assert set(post["responses"]) >= {"202", "401", "403", "422", "503"}
    body = spec["components"]["schemas"]["DeploymentCreate"]
    assert "tenant_id" not in body["properties"]  # the tenant comes from the API key only
    assert set(body["required"]) == {
        "service",
        "environment",
        "version",
        "commit_sha",
        "deployed_at",
    }
    assert body["additionalProperties"] is False


def test_openapi_describes_ingest_and_read_endpoints() -> None:
    app = create_app(
        Settings(
            env="test",
            database_url="postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none",
            kafka_bootstrap="127.0.0.1:1",
            otel_enabled=False,
        )
    )
    with TestClient(app) as client:
        spec = client.get("/openapi.json").json()
    paths = spec["paths"]
    traces = paths["/v1/traces"]["post"]
    assert set(traces["requestBody"]["content"]) == {"application/x-protobuf", "application/json"}
    assert set(traces["responses"]) >= {"200", "400", "401", "403", "413", "415", "503"}
    for path in (
        "/api/v1/deployments/{deployment_id}/analysis",
        "/api/v1/regressions",
        "/api/v1/services",
        "/api/v1/service-map",
    ):
        assert "get" in paths[path], path
    schemas = spec["components"]["schemas"]
    assert "probable_origin" in schemas["AnalysisOut"]["properties"]
    assert "latency_p95_ms_approx" in schemas["ServiceHealth"]["properties"]
    assert "root cause" not in str(spec).lower()  # wording rule


def test_openapi_describes_the_saas_endpoints_without_leaking_secrets() -> None:
    app = create_app(
        Settings(
            env="test",
            database_url="postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none",
            kafka_bootstrap="127.0.0.1:1",
            otel_enabled=False,
        )
    )
    spec = app.openapi()
    paths = spec["paths"]
    for path, method in [
        ("/api/v1/auth/signup", "post"),
        ("/api/v1/auth/login", "post"),
        ("/api/v1/auth/refresh", "post"),
        ("/api/v1/auth/logout", "post"),
        ("/api/v1/auth/password-reset/confirm", "post"),
        ("/api/v1/auth/github/authorize", "get"),
        ("/api/v1/me", "get"),
        ("/api/v1/orgs", "post"),
        ("/api/v1/orgs/{org_id}/members/{user_id}", "patch"),
        ("/api/v1/orgs/{org_id}/api-keys/{key_id}/rotate", "post"),
        ("/api/v1/orgs/{org_id}/audit-log/export.csv", "get"),
        ("/api/v1/orgs/{org_id}/projects/{project_id}/alert-rules", "post"),
        ("/api/v1/orgs/{org_id}/billing/checkout", "post"),
        ("/api/v1/billing/stripe/webhook", "post"),
        ("/api/v1/plans", "get"),
    ]:
        assert method in paths[path], (path, method)
    schemas = spec["components"]["schemas"]
    rule_out = set(schemas["AlertRuleOut"]["properties"])
    assert not rule_out & {"slack_webhook_url", "slack_webhook_ciphertext"}
    assert "slack" in rule_out
    assert not set(schemas["ApiKeyOut"]["properties"]) & {"key", "secret_hash"}
    assert "key" in schemas["ApiKeyCreated"]["properties"]  # shown once, on create/rotate
    assert not set(schemas["SessionOut"]["properties"]) & {"access_token", "refresh_token"}

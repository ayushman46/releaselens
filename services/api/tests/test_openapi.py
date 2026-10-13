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

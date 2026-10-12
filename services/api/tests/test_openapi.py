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

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from rl_api.app import create_app
from rl_api.errors import ApiError
from rl_api.settings import Settings


@pytest.fixture
def settings() -> Settings:
    # Unreachable dependencies on purpose: unit tests must not need Docker.
    return Settings(
        env="test",
        database_url="postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none",
        kafka_bootstrap="127.0.0.1:1",
        readiness_timeout_s=0.5,
        otel_enabled=True,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("kaboom")

    @app.get("/teapot")
    async def teapot() -> None:
        raise ApiError(418, "teapot", "short and stout")

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_healthz_is_dependency_free(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readyz_reports_failing_dependencies_with_503(client: TestClient) -> None:
    resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert set(body["checks"]) == {"postgres", "kafka"}
    assert all(c["status"] == "error" for c in body["checks"].values())


def test_readyz_ok_when_all_checks_pass(client: TestClient) -> None:
    async def ok() -> None:
        return None

    client.app.state.readiness_checks = {"postgres": ok, "kafka": ok}  # type: ignore[attr-defined]
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


def test_request_id_is_generated_and_echoed(client: TestClient) -> None:
    generated = client.get("/healthz").headers["x-request-id"]
    assert len(generated) == 32
    assert (
        client.get("/healthz", headers={"x-request-id": "abc-123"}).headers["x-request-id"]
        == "abc-123"
    )
    # Unsafe ids are replaced, never reflected.
    assert (
        client.get("/healthz", headers={"x-request-id": "bad id\n"}).headers["x-request-id"]
        != "bad id\n"
    )


def test_errors_use_json_envelope_with_request_id(client: TestClient) -> None:
    resp = client.get("/boom", headers={"x-request-id": "req-err"})
    assert resp.status_code == 500
    assert resp.json() == {
        "error": {
            "code": "internal_error",
            "message": "internal server error",
            "request_id": "req-err",
        }
    }
    resp = client.get("/teapot")
    assert resp.status_code == 418
    assert resp.json()["error"]["code"] == "teapot"
    resp = client.get("/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "route_not_found"


def test_unknown_route_is_distinguished_from_a_missing_resource(client: TestClient) -> None:
    """Routing misses answer ``route_not_found``; resource misses (ApiError) keep ``not_found``."""
    for path in ("/api/v1/nope", "/api/v1/deployments/x/y/z", "/api/v2/deployments"):
        resp = client.get(path, headers={"x-request-id": "req-404"})
        assert resp.status_code == 404, path
        assert resp.json() == {
            "error": {
                "code": "route_not_found",
                "message": "no such route",
                "request_id": "req-404",
            }
        }
    # A known path with the wrong method is still a 405, not a 404.
    resp = client.delete("/healthz")
    assert resp.status_code == 405
    assert resp.json()["error"]["code"] == "method_not_allowed"
    # An ApiError 404 from a handler keeps its own code.
    app = client.app

    @app.get("/missing-thing")  # type: ignore[attr-defined, untyped-decorator]
    async def missing() -> None:
        raise ApiError(404, "not_found", "thing not found")

    resp = client.get("/missing-thing")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("configured", [False, True])
def test_github_status_reports_configuration_without_side_effects(
    settings: Settings, configured: bool
) -> None:
    if configured:
        settings = settings.model_copy(
            update={
                "github_oauth_client_id": "Iv1.unit",
                "github_oauth_client_secret": SecretStr("unit-secret"),  # gitleaks:allow
            }
        )
    with TestClient(create_app(settings)) as c:
        resp = c.get("/api/v1/auth/github/status")
    assert resp.status_code == 200
    assert resp.json() == {"configured": configured}
    assert "set-cookie" not in resp.headers  # no OAuth state, no session


def test_access_log_is_json_with_trace_and_request_ids(
    client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    client.get("/does-not-exist", headers={"x-request-id": "req-log"})
    lines = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]
    access = [line for line in lines if line.get("logger") == "rl_api.access"]
    assert access, "expected an access log line"
    entry = access[-1]
    assert entry["request_id"] == "req-log"
    assert entry["status"] == 404
    assert entry["path"] == "/does-not-exist"
    assert len(entry["trace_id"]) == 32
    assert logging.getLogger().handlers  # configured

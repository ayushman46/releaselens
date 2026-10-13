"""The global 1 MiB body limit (M10) must not cut off OTLP ingest (M7), which has its own
larger cap.

Both limits were written on separate branches; this pins the merged behaviour.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from rl_api.app import create_app
from rl_api.settings import Settings

TWO_MIB = b"\x00" * (2 * 1024 * 1024)


def _client() -> TestClient:
    return TestClient(
        create_app(
            Settings(
                env="test",
                database_url="postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none",
                kafka_bootstrap="127.0.0.1:1",
                otel_enabled=False,
            )
        ),
        raise_server_exceptions=False,
    )


def test_otlp_ingest_accepts_bodies_above_the_global_limit() -> None:
    with _client() as client:
        resp = client.post(
            "/v1/traces", content=TWO_MIB, headers={"content-type": "application/x-protobuf"}
        )
    # Reaches the route (and fails auth: no key), instead of the global 413.
    assert resp.status_code == 401


def test_other_routes_keep_the_global_limit() -> None:
    with _client() as client:
        resp = client.post(
            "/api/v1/deployments", content=TWO_MIB, headers={"content-type": "application/json"}
        )
    assert resp.status_code == 413

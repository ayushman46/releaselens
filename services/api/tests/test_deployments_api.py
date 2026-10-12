"""Deployment API against real Postgres + Kafka (spec §10 / test plan item f)."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, TypeVar

import pytest
from confluent_kafka import OFFSET_BEGINNING, Consumer, Message, TopicPartition
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rl_api.apikeys import create_api_key
from rl_api.app import create_app
from rl_api.settings import Settings
from rl_core.events import DEPLOYMENT_EVENTS, DeploymentEvent, parse_envelope
from rl_core.ids import uuid7
from rl_core.tenancy import Principal, TenantContext
from rl_db import create_engine, create_session_factory
from rl_db.admin.identity import create_organization
from rl_db.models import ApiKey
from rl_db.repositories import record_deployment, revoke_api_key

pytestmark = pytest.mark.integration

T = TypeVar("T")
Sessions = async_sessionmaker[AsyncSession]


def _db[T](url: str, fn: Callable[[Sessions], Awaitable[T]]) -> T:
    async def go() -> T:
        engine = create_engine(url, pool_size=2)
        try:
            return await fn(create_session_factory(engine))
        finally:
            await engine.dispose()

    return asyncio.run(go())


@dataclass
class Tenant:
    id: uuid.UUID
    keys: dict[str, str] = field(default_factory=dict)  # label -> plaintext
    key_ids: dict[str, uuid.UUID] = field(default_factory=dict)

    def auth(self, label: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.keys[label]}"}


def _make_tenant(url: str) -> Tenant:
    async def go(sessions: Sessions) -> Tenant:
        async with sessions.begin() as s:
            org = await create_organization(s, name="Acme", slug=f"acme-{uuid.uuid4().hex[:12]}")
            tenant = Tenant(id=org.id)
            specs: dict[str, tuple[list[str], datetime | None]] = {
                "writer": (["deployments:write"], None),
                "reader": (["read"], None),
                "all": (["deployments:write", "read"], None),
                "expired": (["deployments:write", "read"], datetime.now(UTC) - timedelta(1)),
                "revoked": (["deployments:write", "read"], None),
            }
            for label, (scopes, expires_at) in specs.items():
                created = await create_api_key(s, org.id, label, scopes, expires_at=expires_at)
                tenant.keys[label] = created.plaintext
                tenant.key_ids[label] = created.id
            ctx = TenantContext(tenant_id=org.id, principal=Principal.SYSTEM)
            await revoke_api_key(s, ctx, tenant.key_ids["revoked"])
        return tenant

    return _db(url, go)


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "service": "payment",
        "environment": "production",
        "version": "v2.0.1",
        "previous_version": "v2.0.0",
        "commit_sha": "0123456789abcdef0123456789abcdef01234567",
        "deployed_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        "deployer": "ci",
        "ci_run_url": "https://ci.example.com/runs/1",
    }
    body.update(overrides)
    return body


def _find_event(bootstrap: str, event_id: str, timeout_s: float = 20.0) -> Message:
    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": f"test-{uuid.uuid4()}",
            "enable.auto.commit": False,
        }
    )
    try:
        md = consumer.list_topics(DEPLOYMENT_EVENTS, timeout=10)
        consumer.assign(
            [
                TopicPartition(DEPLOYMENT_EVENTS, p, OFFSET_BEGINNING)
                for p in md.topics[DEPLOYMENT_EVENTS].partitions
            ]
        )
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            msg = consumer.poll(0.5)
            if msg is None or msg.error():
                continue
            if json.loads(msg.value() or b"{}").get("event_id") == event_id:
                return msg
        raise AssertionError(f"event {event_id} not found on {DEPLOYMENT_EVENTS}")
    finally:
        consumer.close()


@pytest.fixture(scope="module")
def settings(api_db_url: str, kafka_bootstrap: str) -> Settings:
    return Settings(
        env="test",
        database_url=api_db_url,
        kafka_bootstrap=kafka_bootstrap,
        otel_enabled=False,
        kafka_delivery_timeout_s=5.0,
    )


@pytest.fixture(scope="module")
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as c:
        yield c


@pytest.fixture(scope="module")
def tenant_a(api_db_url: str) -> Tenant:
    return _make_tenant(api_db_url)


@pytest.fixture(scope="module")
def tenant_b(api_db_url: str) -> Tenant:
    return _make_tenant(api_db_url)


def _assert_401(resp: Any) -> None:
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "unauthorized"
    assert resp.json()["error"]["message"] == "missing or invalid API key"
    assert resp.headers["www-authenticate"].startswith("Bearer")


# ----------------------------------------------------------------------------- authn / authz
def test_post_without_key_is_401(client: TestClient) -> None:
    _assert_401(client.post("/api/v1/deployments", json=_body()))


def test_every_bad_key_gets_the_same_401(client: TestClient, tenant_a: Tenant) -> None:
    good = tenant_a.keys["writer"]
    flipped = good[:-1] + ("A" if good[-1] != "A" else "B")
    unknown = "rlk_" + "Z" * 12 + good[16:]
    for headers in [
        {"Authorization": "Bearer nonsense"},
        {"Authorization": f"Basic {good}"},
        {"Authorization": f"Bearer {flipped}"},
        {"Authorization": f"Bearer {unknown}"},
        {"x-releaselens-key": flipped},
        {"Authorization": f"Bearer {good}", "x-releaselens-key": tenant_a.keys["reader"]},
        tenant_a.auth("expired"),
        tenant_a.auth("revoked"),
    ]:
        _assert_401(client.post("/api/v1/deployments", json=_body(), headers=headers))
        _assert_401(client.get("/api/v1/deployments", headers=headers))


def test_wrong_scope_is_403(client: TestClient, tenant_a: Tenant) -> None:
    resp = client.post("/api/v1/deployments", json=_body(), headers=tenant_a.auth("reader"))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "insufficient_scope"
    resp = client.get("/api/v1/deployments", headers=tenant_a.auth("writer"))
    assert resp.status_code == 403


# ----------------------------------------------------------------------------- intake
def test_valid_post_is_202_and_lands_on_the_topic_keyed_by_tenant_and_service(
    client: TestClient, tenant_a: Tenant, kafka_bootstrap: str
) -> None:
    resp = client.post(
        "/api/v1/deployments",
        json=_body(repository="acme/payment", pull_request=42, labels={"team": "payments"}),
        headers=tenant_a.auth("writer"),
    )
    assert resp.status_code == 202, resp.text
    event_id = resp.json()["event_id"]
    assert uuid.UUID(event_id).version == 7

    msg = _find_event(kafka_bootstrap, event_id)
    assert msg.key() == f"{tenant_a.id}:payment".encode()
    headers = dict(msg.headers() or [])
    assert headers["rl-event-type"] == b"deployment.completed"
    assert headers["rl-schema-version"] == b"2"
    event = parse_envelope(msg.value())
    assert event.tenant_id == tenant_a.id  # from the key, never from the request
    assert event.schema_version == 2
    assert (event.repository, event.pull_request) == ("acme/payment", 42)
    assert dict(event.labels) == {"team": "payments"}


def test_alternate_header_and_client_event_id(
    client: TestClient, tenant_a: Tenant, kafka_bootstrap: str
) -> None:
    event_id = str(uuid.uuid4())  # any UUID version is accepted
    resp = client.post(
        "/api/v1/deployments",
        json=_body(event_id=event_id),
        headers={"x-releaselens-key": tenant_a.keys["writer"]},
    )
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"event_id": event_id}
    assert parse_envelope(_find_event(kafka_bootstrap, event_id).value()).event_id == uuid.UUID(
        event_id
    )


def test_schema_v1_is_produced_as_v1(
    client: TestClient, tenant_a: Tenant, kafka_bootstrap: str
) -> None:
    resp = client.post(
        "/api/v1/deployments", json=_body(schema_version=1), headers=tenant_a.auth("writer")
    )
    assert resp.status_code == 202, resp.text
    msg = _find_event(kafka_bootstrap, resp.json()["event_id"])
    assert json.loads(msg.value() or b"")["schema_version"] == 1
    assert dict(msg.headers() or [])["rl-schema-version"] == b"1"


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"schema_version": 1, "labels": {"a": "b"}}, "schema_version 1 does not support"),
        ({"schema_version": 3}, "schema_version"),
        ({"schema_version": True}, "schema_version"),
        ({"tenant_id": str(uuid.uuid4())}, "tenant_id"),
        ({"commit_sha": "not-hex"}, "commit_sha"),
        ({"pull_request": "12"}, "pull_request"),
        ({"deployed_at": "2026-10-13T12:00:00"}, "deployed_at"),
        ({"deployed_at": (datetime.now(UTC) + timedelta(days=2)).isoformat()}, "future"),
    ],
)
def test_invalid_bodies_are_422(
    client: TestClient, tenant_a: Tenant, overrides: dict[str, Any], fragment: str
) -> None:
    resp = client.post(
        "/api/v1/deployments", json=_body(**overrides), headers=tenant_a.auth("writer")
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "validation_error"
    assert fragment in resp.text


def test_kafka_down_is_503_never_202(settings: Settings, tenant_a: Tenant) -> None:
    dead = settings.model_copy(
        update={"kafka_bootstrap": "127.0.0.1:1", "kafka_delivery_timeout_s": 1.0}
    )
    with TestClient(create_app(dead)) as c:
        start = time.perf_counter()
        resp = c.post("/api/v1/deployments", json=_body(), headers=tenant_a.auth("writer"))
        elapsed = time.perf_counter() - start
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "event_bus_unavailable"
    assert uuid.UUID(resp.json()["error"]["details"]["event_id"]).version == 7
    assert resp.headers["retry-after"] == "5"
    assert elapsed < 5.0  # bounded by kafka_delivery_timeout_s (+ grace), never hangs


# ----------------------------------------------------------------------------- reads
def _seed(url: str, tenant: Tenant, n: int, service: str = "payment") -> list[uuid.UUID]:
    async def go(sessions: Sessions) -> list[uuid.UUID]:
        ctx = TenantContext(tenant_id=tenant.id, principal=Principal.SYSTEM)
        ids = []
        async with sessions.begin() as s:
            for i in range(n):
                rec = await record_deployment(
                    s,
                    ctx,
                    DeploymentEvent(
                        event_id=uuid7(),
                        tenant_id=tenant.id,
                        occurred_at=datetime.now(UTC),
                        schema_version=2,
                        service=service,
                        environment="production",
                        version=f"v{i}",
                        previous_version=None,
                        commit_sha="abcdef1",
                        deployed_at=datetime(2026, 10, 1, tzinfo=UTC) + timedelta(minutes=i),
                        deployer=None,
                        ci_run_url=None,
                        repository=None,
                        pull_request=None,
                        risk_assessment_id=None,
                        labels=MappingProxyType({}),
                    ),
                )
                assert rec.deployment_id is not None
                ids.append(rec.deployment_id)
        return ids

    return _db(url, go)


def test_get_by_id_is_tenant_scoped(
    client: TestClient, api_db_url: str, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    [a_id] = _seed(api_db_url, tenant_a, 1)
    resp = client.get(f"/api/v1/deployments/{a_id}", headers=tenant_a.auth("reader"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(a_id)
    assert {j["kind"] for j in body["analysis_jobs"]} == {"early_5m", "early_10m", "final"}
    assert all(j["status"] == "pending" for j in body["analysis_jobs"])

    other = client.get(f"/api/v1/deployments/{a_id}", headers=tenant_b.auth("reader"))
    missing = client.get(f"/api/v1/deployments/{uuid.uuid4()}", headers=tenant_b.auth("reader"))
    assert other.status_code == missing.status_code == 404
    assert other.json()["error"]["code"] == missing.json()["error"]["code"] == "not_found"
    assert other.json()["error"]["message"] == missing.json()["error"]["message"]


def test_list_is_tenant_scoped_and_paginates(
    client: TestClient, api_db_url: str, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    b_ids = set(_seed(api_db_url, tenant_b, 5, service="orders-b"))
    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 2, "service": "orders-b"}
        if cursor:
            params["cursor"] = cursor
        resp = client.get("/api/v1/deployments", params=params, headers=tenant_b.auth("reader"))
        assert resp.status_code == 200, resp.text
        pages += 1
        seen += [item["id"] for item in resp.json()["items"]]
        cursor = resp.json()["next_cursor"]
        if cursor is None:
            break
    assert pages == 3
    assert {uuid.UUID(i) for i in seen} == b_ids

    a_view = client.get(
        "/api/v1/deployments", params={"limit": 200}, headers=tenant_a.auth("reader")
    ).json()
    assert not {uuid.UUID(i["id"]) for i in a_view["items"]} & b_ids
    assert (
        client.get(
            "/api/v1/deployments", params={"service": "orders-b"}, headers=tenant_a.auth("reader")
        ).json()["items"]
        == []
    )


def test_bad_cursor_is_400(client: TestClient, tenant_a: Tenant) -> None:
    resp = client.get(
        "/api/v1/deployments", params={"cursor": "%%%"}, headers=tenant_a.auth("reader")
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_cursor"


def test_last_used_at_is_recorded_after_a_successful_request(
    client: TestClient, api_db_url: str, tenant_a: Tenant
) -> None:
    key_id = tenant_a.key_ids["all"]

    async def last_used(sessions: Sessions) -> datetime | None:
        async with sessions() as s:
            return await s.scalar(select(ApiKey.last_used_at).where(ApiKey.id == key_id))

    assert _db(api_db_url, last_used) is None
    assert client.get("/api/v1/deployments", headers=tenant_a.auth("all")).status_code == 200
    first = _db(api_db_url, last_used)
    assert first is not None
    # Throttled: a second request within the interval does not write again.
    assert client.get("/api/v1/deployments", headers=tenant_a.auth("all")).status_code == 200
    assert _db(api_db_url, last_used) == first


def test_access_log_carries_the_authenticated_tenant(
    client: TestClient, tenant_a: Tenant, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    client.get("/api/v1/deployments", headers=tenant_a.auth("reader"))
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]
    access = [x for x in lines if x.get("logger") == "rl_api.access"]
    assert access, "expected an access log line"
    assert access[-1]["tenant_id"] == str(tenant_a.id)
    assert access[-1]["api_key_id"] == str(tenant_a.key_ids["reader"])
    assert tenant_a.keys["reader"] not in json.dumps(lines)  # the key itself is never logged

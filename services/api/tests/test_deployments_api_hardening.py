"""Adversarial API cases: crafted cursors and headers, key lifecycle races, tenant probing.

Every failure here must be a clean 4xx with the JSON error envelope. A 500 on attacker-
controlled input is a bug even when nothing leaks: it pages someone, and its timing and
logging differ from the 4xx path.
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, TypeVar

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rl_api import apikeys, auth
from rl_api.apikeys import create_api_key
from rl_api.app import create_app
from rl_api.settings import Settings
from rl_core.events import DeploymentEvent
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


def _tenant_with_key(url: str, scopes: list[str]) -> tuple[uuid.UUID, uuid.UUID, str]:
    """``(tenant id, key id, plaintext key)``."""

    async def go(sessions: Sessions) -> tuple[uuid.UUID, uuid.UUID, str]:
        async with sessions.begin() as s:
            org = await create_organization(s, name="Acme", slug=f"acme-{uuid.uuid4().hex[:12]}")
            key = await create_api_key(s, org.id, "k", scopes)
        return org.id, key.id, key.plaintext

    return _db(url, go)


def _seed_one(url: str, tenant_id: uuid.UUID, service: str) -> uuid.UUID:
    async def go(sessions: Sessions) -> uuid.UUID:
        ctx = TenantContext(tenant_id=tenant_id, principal=Principal.SYSTEM)
        async with sessions.begin() as s:
            rec = await record_deployment(
                s,
                ctx,
                DeploymentEvent(
                    event_id=uuid7(),
                    tenant_id=tenant_id,
                    occurred_at=datetime.now(UTC),
                    schema_version=2,
                    service=service,
                    environment="production",
                    version="v1",
                    previous_version=None,
                    commit_sha="abcdef1",
                    deployed_at=datetime(2026, 10, 1, tzinfo=UTC),
                    deployer=None,
                    ci_run_url=None,
                    repository=None,
                    pull_request=None,
                    risk_assessment_id=None,
                    labels=MappingProxyType({}),
                ),
            )
        assert rec.deployment_id is not None
        return rec.deployment_id

    return _db(url, go)


@pytest.fixture(scope="module")
def client(api_db_url: str, kafka_bootstrap: str) -> Iterator[TestClient]:
    settings = Settings(
        env="test",
        database_url=api_db_url,
        kafka_bootstrap=kafka_bootstrap,
        otel_enabled=False,
        kafka_delivery_timeout_s=5.0,
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as c:
        yield c


def _nested(depth: int) -> Any:
    value: Any = []
    for _ in range(depth):
        value = [value]
    return value


_NESTED = _nested(60)


def _cursor(obj: Any) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


@pytest.mark.parametrize(
    "decoded",
    [
        {"t": "2026-10-13T12:00:00+00:00", "id": 123},
        {"t": "2026-10-13T12:00:00+00:00", "id": ["not", "a", "uuid"]},
        {"t": "2026-10-13T12:00:00+00:00", "id": None},
        {"t": ["2026"], "id": str(uuid.uuid4())},
        {"t": "0001-01-01T00:00:00+14:00", "id": str(uuid.uuid4())},  # underflows in UTC
        {"t": "9999-12-31T23:59:59-14:00", "id": str(uuid.uuid4())},  # overflows in UTC
        _NESTED,
        "a string",
        42,
    ],
)
def test_crafted_cursors_are_400_never_500(
    client: TestClient, api_db_url: str, decoded: Any
) -> None:
    _, _, key = _tenant_with_key(api_db_url, ["read"])
    resp = client.get(
        "/api/v1/deployments",
        params={"cursor": _cursor(decoded)},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid_cursor"


@pytest.mark.parametrize(
    "headers",
    [
        # Two different keys, one of them not ASCII: hmac.compare_digest rejects non-ASCII str.
        [(b"authorization", b"Bearer rlk_\xe9\xe9\xe9"), (b"x-releaselens-key", b"rlk_abc")],
        [(b"authorization", b"Bearer abc"), (b"x-releaselens-key", b"\xff\xfe")],
        [(b"authorization", b"Bearer \xe9")],
        [(b"x-releaselens-key", b"rlk_" + b"\xe9" * 12 + b"_" + b"a" * 43)],
    ],
)
def test_non_ascii_key_headers_are_401_never_500(
    client: TestClient, headers: list[tuple[bytes, bytes]]
) -> None:
    resp = client.get("/api/v1/deployments", headers=headers)
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "unauthorized"


def test_every_rejection_path_costs_exactly_one_hash(
    client: TestClient, api_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Timing-equivalence proxy: an unknown prefix, a wrong secret and a revoked or expired
    key each compute the SHA-256 once, so they cannot be told apart by hashing work. Only a
    malformed key (whose format is public anyway) is rejected before any work."""
    tenant, key_id, key = _tenant_with_key(api_db_url, ["read"])
    calls: list[str] = []
    real = apikeys.hash_api_key

    def counting(plaintext: str) -> str:
        calls.append(plaintext)
        return real(plaintext)

    monkeypatch.setattr(auth, "hash_api_key", counting)

    def hashes_for(presented: str) -> int:
        calls.clear()
        resp = client.get("/api/v1/deployments", headers={"Authorization": f"Bearer {presented}"})
        assert resp.status_code == 401, resp.text
        return len(calls)

    unknown_prefix = "rlk_" + "Q" * 12 + key[16:]
    wrong_secret = key[:-1] + ("A" if key[-1] != "A" else "B")
    assert hashes_for(unknown_prefix) == 1
    assert hashes_for(wrong_secret) == 1
    assert hashes_for("rlk_short") == 0

    async def expire(sessions: Sessions) -> None:
        async with sessions.begin() as s:
            await s.execute(
                update(ApiKey)
                .where(ApiKey.id == key_id)
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )

    _db(api_db_url, expire)
    assert hashes_for(key) == 1
    assert tenant  # the key belonged to a real tenant all along


def test_revocation_and_expiry_apply_to_the_very_next_request(
    client: TestClient, api_db_url: str
) -> None:
    tenant, key_id, key = _tenant_with_key(api_db_url, ["read"])
    auth_header = {"Authorization": f"Bearer {key}"}
    assert client.get("/api/v1/deployments", headers=auth_header).status_code == 200

    async def revoke(sessions: Sessions) -> bool:
        async with sessions.begin() as s:
            ctx = TenantContext(tenant_id=tenant, principal=Principal.SYSTEM)
            return await revoke_api_key(s, ctx, key_id)

    assert _db(api_db_url, revoke) is True
    assert client.get("/api/v1/deployments", headers=auth_header).status_code == 401
    assert _db(api_db_url, revoke) is False  # already revoked: idempotent, no second write

    # A revocation stamped "in the future" (the revoking host's clock runs ahead) still
    # revokes at once: the API must not compare revoked_at against its own clock.
    _, skewed_id, skewed_key = _tenant_with_key(api_db_url, ["read"])

    async def revoke_ahead(sessions: Sessions) -> None:
        async with sessions.begin() as s:
            await s.execute(
                update(ApiKey)
                .where(ApiKey.id == skewed_id)
                .values(revoked_at=datetime.now(UTC) + timedelta(minutes=5))
            )

    _db(api_db_url, revoke_ahead)
    skewed = {"Authorization": f"Bearer {skewed_key}"}
    assert client.get("/api/v1/deployments", headers=skewed).status_code == 401

    # Another tenant cannot revoke (or even detect) this tenant's key by id.
    other, _, _ = _tenant_with_key(api_db_url, ["read"])
    _, victim_id, victim_key = _tenant_with_key(api_db_url, ["read"])

    async def foreign_revoke(sessions: Sessions) -> bool:
        async with sessions.begin() as s:
            ctx = TenantContext(tenant_id=other, principal=Principal.SYSTEM)
            return await revoke_api_key(s, ctx, victim_id)

    assert _db(api_db_url, foreign_revoke) is False
    victim = {"Authorization": f"Bearer {victim_key}"}
    assert client.get("/api/v1/deployments", headers=victim).status_code == 200


def _error_without_request_id(resp: Any) -> dict[str, Any]:
    return {k: v for k, v in resp.json()["error"].items() if k != "request_id"}


def test_tenant_probing_through_ids_filters_and_body_fields(
    client: TestClient, api_db_url: str, kafka_bootstrap: str
) -> None:
    a, _, a_key = _tenant_with_key(api_db_url, ["read", "deployments:write"])
    _, _, b_key = _tenant_with_key(api_db_url, ["read", "deployments:write"])
    a_dep = _seed_one(api_db_url, a, service="secret-service-a")
    b_auth = {"Authorization": f"Bearer {b_key}"}

    # By id: indistinguishable from an id that exists nowhere (status, code, message, keys).
    other = client.get(f"/api/v1/deployments/{a_dep}", headers=b_auth)
    nowhere = client.get(f"/api/v1/deployments/{uuid.uuid4()}", headers=b_auth)
    assert other.status_code == nowhere.status_code == 404
    assert _error_without_request_id(other) == _error_without_request_id(nowhere)

    # By filter: B naming A's service sees nothing.
    by_service = client.get(
        "/api/v1/deployments", params={"service": "secret-service-a"}, headers=b_auth
    )
    assert by_service.status_code == 200
    assert by_service.json() == {"items": [], "next_cursor": None}

    # By cursor: a cursor positioned on A's row still only pages through B's rows.
    cursor = _cursor({"t": "2026-10-01T00:00:01+00:00", "id": str(a_dep)})
    paged = client.get("/api/v1/deployments", params={"cursor": cursor}, headers=b_auth)
    assert paged.status_code == 200
    assert str(a_dep) not in paged.text

    # By body: every way of naming a tenant in the request is refused, not ignored.
    base = {
        "service": "payment",
        "environment": "production",
        "version": "v1",
        "commit_sha": "abcdef1",
        "deployed_at": datetime.now(UTC).isoformat(),
    }
    for extra in ({"tenant_id": str(a)}, {"tenantId": str(a)}, {"payload": {"tenant_id": str(a)}}):
        resp = client.post("/api/v1/deployments", json={**base, **extra}, headers=b_auth)
        assert resp.status_code == 422, resp.text
    # A query-string or header tenant is simply not an input.
    resp = client.post(
        "/api/v1/deployments",
        params={"tenant_id": str(a)},
        json=base,
        headers={**b_auth, "x-tenant-id": str(a)},
    )
    assert resp.status_code == 202, resp.text
    assert a_key  # tenant A's key exists and was never used by B


def test_log_context_does_not_leak_between_requests(
    client: TestClient, api_db_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    tenant, _, key = _tenant_with_key(api_db_url, ["read"])
    capsys.readouterr()
    client.get("/api/v1/deployments", headers={"Authorization": f"Bearer {key}"})
    client.get("/api/v1/deployments")  # anonymous, right after
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]
    access = [x for x in lines if x.get("logger") == "rl_api.access"]
    assert [x["status"] for x in access[-2:]] == [200, 401]
    assert access[-2]["tenant_id"] == str(tenant)
    assert "tenant_id" not in access[-1]
    assert "api_key_id" not in access[-1]

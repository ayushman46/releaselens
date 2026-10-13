"""Row-level security (ADR-0010 item 3, ADR-0023) against a real, migrated Postgres 16.

The repositories already filter by tenant; these tests show that Postgres filters too, for
statements that forget to (raw SQL with no ``WHERE tenant_id``).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import MappingProxyType

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from rl_core.events import DeploymentEvent
from rl_core.ids import uuid7
from rl_core.tenancy import Principal, TenantContext
from rl_db import create_engine, create_session_factory
from rl_db.admin import identity
from rl_db.models import Deployment
from rl_db.repositories import list_deployments, record_deployment
from rl_db.repositories._tenancy import bind_tenant

pytestmark = pytest.mark.integration

Sessions = async_sessionmaker[AsyncSession]
# tenant_id is nullable by design here (rows exist before a tenant is known): admin-only tables.
ADMIN_ONLY = {"webhook_deliveries", "stripe_events", "audit_log"}


@pytest.fixture
async def engine(migrated_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(migrated_url, pool_size=2)
    yield eng
    await eng.dispose()


@pytest.fixture
def sessions(engine: AsyncEngine) -> Sessions:
    return create_session_factory(engine)


async def _tenant_with_deployment(sessions: Sessions, service: str) -> TenantContext:
    async with sessions.begin() as s:
        org = await identity.create_organization(
            s, name=service, slug=f"rls-{uuid.uuid4().hex[:10]}"
        )
    ctx = TenantContext(tenant_id=org.id, principal=Principal.SYSTEM)
    event = DeploymentEvent(
        event_id=uuid7(),
        tenant_id=ctx.tenant_id,
        occurred_at=datetime(2026, 10, 13, 12, tzinfo=UTC),
        schema_version=2,
        service=service,
        environment="production",
        version="v2",
        previous_version="v1",
        commit_sha="abcdef1",
        deployed_at=datetime(2026, 10, 13, 12, tzinfo=UTC),
        deployer=None,
        ci_run_url=None,
        repository=None,
        pull_request=None,
        risk_assessment_id=None,
        labels=MappingProxyType({}),
    )
    async with sessions.begin() as s:
        await record_deployment(s, ctx, event)
    return ctx


async def test_every_tenant_owned_table_has_rls_and_a_policy(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT c.relname, c.relrowsecurity, a.attnotnull,
                           EXISTS (SELECT FROM pg_policy p WHERE p.polrelid = c.oid
                                   AND p.polname = 'tenant_isolation') AS has_policy
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
                    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id'
                    WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition
                    """
                )
            )
        ).all()
    assert rows, "no tenant-owned tables found"
    missing = sorted(
        name
        for name, rls, not_null, has_policy in rows
        if name not in ADMIN_ONLY and not (rls and has_policy and not_null)
    )
    assert missing == [], f"tenant-owned tables without RLS + tenant_isolation: {missing}"
    assert {name for name, *_ in rows} >= ADMIN_ONLY  # the exemptions are still real tables


async def test_a_bound_transaction_sees_only_its_tenant_even_without_a_where(
    sessions: Sessions,
) -> None:
    a = await _tenant_with_deployment(sessions, "rls-a")
    b = await _tenant_with_deployment(sessions, "rls-b")
    async with sessions() as s:
        everyone = await s.scalar(select(func.count()).select_from(Deployment))
        await bind_tenant(s, a)
        seen = (await s.execute(select(Deployment.tenant_id).distinct())).scalars().all()
        assert (await s.scalar(text("SELECT current_user"))) == "rl_tenant"
    assert everyone is not None
    assert everyone >= 2  # the owner role is not filtered
    assert seen == [a.tenant_id]
    async with sessions() as s:  # a new transaction starts unbound again
        await bind_tenant(s, b)
        assert (await s.execute(select(Deployment.tenant_id).distinct())).scalars().all() == [
            b.tenant_id
        ]


async def test_repository_calls_are_bound(sessions: Sessions) -> None:
    a = await _tenant_with_deployment(sessions, "rls-c")
    async with sessions() as s:
        page = await list_deployments(s, a, limit=10)
        assert [d.tenant_id for d in page] == [a.tenant_id]
        assert (await s.scalar(text("SELECT current_setting('app.tenant_id')"))) == str(a.tenant_id)


async def test_rl_tenant_without_a_tenant_sees_nothing(sessions: Sessions) -> None:
    await _tenant_with_deployment(sessions, "rls-d")
    async with sessions() as s:
        await s.execute(text("SET LOCAL ROLE rl_tenant"))
        assert await s.scalar(select(func.count()).select_from(Deployment)) == 0


async def test_writes_into_another_tenant_are_rejected_by_postgres(sessions: Sessions) -> None:
    a = await _tenant_with_deployment(sessions, "rls-e")
    b = await _tenant_with_deployment(sessions, "rls-f")
    async with sessions() as s:
        await bind_tenant(s, a)
        with pytest.raises(DBAPIError, match="row-level security"):
            await s.execute(
                text(
                    "INSERT INTO deployments (id, tenant_id, event_id, schema_version, service,"
                    " environment, version, commit_sha, occurred_at, deployed_at)"
                    " VALUES (:id, :t, :e, 2, 'x', 'production', 'v1', 'abcdef1', now(), now())"
                ),
                {"id": uuid7(), "t": b.tenant_id, "e": uuid7()},
            )
    async with sessions() as s:
        await bind_tenant(s, a)
        moved = await s.execute(
            text("UPDATE deployments SET service = 'stolen' WHERE tenant_id = :t"),
            {"t": b.tenant_id},
        )
        assert getattr(moved, "rowcount", None) == 0  # B's rows are invisible, so untouched

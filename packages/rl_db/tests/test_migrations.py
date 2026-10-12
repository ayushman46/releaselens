"""Migrations must (1) apply cleanly, (2) match the ORM models exactly, (3) fully reverse."""

from __future__ import annotations

import asyncio

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, inspect
from sqlalchemy.ext.asyncio import create_async_engine

import rl_db.models  # noqa: F401
from rl_db.base import Base
from rl_db.migrate import make_config

pytestmark = pytest.mark.integration


def _diff(conn: Connection) -> list[object]:
    ctx = MigrationContext.configure(conn, opts={"compare_type": True})
    return list(compare_metadata(ctx, Base.metadata))


def _tables(conn: Connection) -> set[str]:
    return set(inspect(conn).get_table_names())


async def _run(url: str, fn):  # type: ignore[no-untyped-def]
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(fn)
    finally:
        await engine.dispose()


def test_upgrade_matches_models_and_downgrade_reverses(pg_url: str) -> None:
    cfg = make_config(pg_url)

    command.upgrade(cfg, "head")
    tables = asyncio.run(_run(pg_url, _tables))
    assert {
        "organizations",
        "users",
        "memberships",
        "api_keys",
        "deployments",
        "analysis_jobs",
        "outbox",
    } <= tables

    diff = asyncio.run(_run(pg_url, _diff))
    assert diff == [], f"models and migrations disagree: {diff}"

    command.downgrade(cfg, "base")
    assert asyncio.run(_run(pg_url, _tables)) <= {"alembic_version"}

    # Re-applying after a full downgrade must also work (idempotent round-trip).
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")


def _enum_types(conn: Connection) -> set[str]:
    rows = conn.exec_driver_sql("SELECT typname FROM pg_type WHERE typtype = 'e'").all()
    return {r[0] for r in rows}


def _seed_m6_rows(conn: Connection) -> None:
    """One organization, deployment, job and outbox row, as 0002 creates them."""
    conn.exec_driver_sql(
        "INSERT INTO organizations (id, name, slug) "
        "VALUES ('00000000-0000-7000-8000-000000000001', 'Acme', 'acme')"
    )
    conn.exec_driver_sql(
        "INSERT INTO deployments (id, tenant_id, event_id, schema_version, service, "
        "environment, version, commit_sha, occurred_at, deployed_at) VALUES ("
        "'00000000-0000-7000-8000-000000000002', '00000000-0000-7000-8000-000000000001', "
        "'00000000-0000-7000-8000-000000000003', 2, 'payment', 'production', 'v2', 'abcdef1', "
        "now(), now())"
    )
    conn.exec_driver_sql(
        "INSERT INTO analysis_jobs (id, tenant_id, deployment_id, kind, run_at) VALUES ("
        "'00000000-0000-7000-8000-000000000004', '00000000-0000-7000-8000-000000000001', "
        "'00000000-0000-7000-8000-000000000002', 'final', now())"
    )
    conn.exec_driver_sql(
        "INSERT INTO outbox (id, tenant_id, topic, key, payload) VALUES ("
        "'00000000-0000-7000-8000-000000000005', '00000000-0000-7000-8000-000000000001', "
        "'analysis.results', 'k', '{}'::jsonb)"
    )
    conn.commit()


def test_0002_steps_down_to_0001_with_data_present_and_back_up(pg_url: str) -> None:
    """Downgrading only the M6 revision, on a database that has M6 rows, must drop exactly
    what 0002 added (tables *and* enum types) and keep the 0001 schema and its data.
    Upgrading again must land on a schema identical to the models."""
    cfg = make_config(pg_url)
    m6_tables = {"deployments", "analysis_jobs", "outbox"}
    m6_enums = {"analysis_job_kind", "analysis_job_status"}

    command.upgrade(cfg, "head")
    asyncio.run(_run(pg_url, _seed_m6_rows))
    command.downgrade(cfg, "0001_baseline")

    tables = asyncio.run(_run(pg_url, _tables))
    assert not tables & m6_tables
    assert {"organizations", "users", "memberships", "api_keys"} <= tables
    assert not asyncio.run(_run(pg_url, _enum_types)) & m6_enums

    def org_count(conn: Connection) -> int:
        return int(conn.exec_driver_sql("SELECT count(*) FROM organizations").scalar_one())

    assert asyncio.run(_run(pg_url, org_count)) == 1  # 0001 data survives the step down

    command.upgrade(cfg, "head")
    assert m6_tables <= asyncio.run(_run(pg_url, _tables))
    assert m6_enums <= asyncio.run(_run(pg_url, _enum_types))
    diff = asyncio.run(_run(pg_url, _diff))
    assert diff == [], f"models and migrations disagree after a step down/up: {diff}"

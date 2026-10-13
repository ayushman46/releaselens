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


M10_TABLES = {
    "auth_sessions",
    "auth_refresh_tokens",
    "user_tokens",
    "oauth_states",
    "invitations",
    "projects",
    "services",
    "environments",
    "audit_log",
    "alert_rules",
    "notification_log",
    "subscriptions",
    "stripe_events",
    "usage_counters",
    "rate_limit_buckets",
}

M5_TABLES = {
    "ci_runs",
    "risk_assessments",
    "github_installations",
    "webhook_deliveries",
    "github_pull_requests",
}


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
        *M10_TABLES,
        *M5_TABLES,
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


def _functions(conn: Connection) -> set[str]:
    rows = conn.exec_driver_sql(
        "SELECT proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public'"
    ).all()
    return {r[0] for r in rows}


def _seed_m10_rows(conn: Connection) -> None:
    """Rows in the 0001 tables plus 0003 tables (an audit entry, a project)."""
    conn.exec_driver_sql(
        "INSERT INTO organizations (id, name, slug) "
        "VALUES ('00000000-0000-7000-8000-000000000011', 'Beta', 'beta')"
    )
    conn.exec_driver_sql(
        "INSERT INTO projects (id, tenant_id, name, slug) VALUES ("
        "'00000000-0000-7000-8000-000000000012', '00000000-0000-7000-8000-000000000011', "
        "'Web', 'web')"
    )
    conn.exec_driver_sql(
        "INSERT INTO audit_log (id, tenant_id, actor_type, action) VALUES ("
        "'00000000-0000-7000-8000-000000000013', '00000000-0000-7000-8000-000000000011', "
        "'system', 'org.created')"
    )
    conn.commit()


def test_0003_steps_down_to_0002_with_data_present_and_back_up(pg_url: str) -> None:
    """Downgrading only the M10 revision drops its tables and the audit trigger function,
    keeps the 0001/0002 schema and data, and leaves the shared enum types alone."""
    cfg = make_config(pg_url)
    command.upgrade(cfg, "head")
    asyncio.run(_run(pg_url, _seed_m10_rows))
    command.downgrade(cfg, "0002_deployments_jobs_outbox")

    tables = asyncio.run(_run(pg_url, _tables))
    assert not tables & M10_TABLES
    assert {"organizations", "users", "memberships", "api_keys", "deployments"} <= tables
    assert "audit_log_immutable" not in asyncio.run(_run(pg_url, _functions))
    assert {"role", "plan"} <= asyncio.run(_run(pg_url, _enum_types))  # 0001's types survive

    def org_count(conn: Connection) -> int:
        return int(conn.exec_driver_sql("SELECT count(*) FROM organizations").scalar_one())

    assert asyncio.run(_run(pg_url, org_count)) == 1

    command.upgrade(cfg, "head")
    assert asyncio.run(_run(pg_url, _tables)) >= M10_TABLES
    diff = asyncio.run(_run(pg_url, _diff))
    assert diff == [], f"models and migrations disagree after a step down/up: {diff}"


def _seed_m5_rows(conn: Connection) -> None:
    """A CI run, an assessment, an installation and a delivery (what 0005 adds)."""
    org = "00000000-0000-7000-8000-000000000021"
    conn.exec_driver_sql(
        f"INSERT INTO organizations (id, name, slug) VALUES ('{org}', 'G', 'gamma')"
    )
    conn.exec_driver_sql(
        "INSERT INTO ci_runs (id, tenant_id, repository, commit_sha, started_at, finished_at, "
        f"status, source, external_id) VALUES ('00000000-0000-7000-8000-000000000022', '{org}', "
        "'acme/app', 'abc', now() - interval '5 minutes', now(), 'passed', 'api', 'r1')"
    )
    conn.exec_driver_sql(
        "INSERT INTO risk_assessments (id, tenant_id, repository, commit_sha, event, "
        "triggered_at, features, change_facts, model_name, model_version, "
        "feature_schema_version, raw_score, risk_score, risk_band, shap, history_used, "
        f"cold_start) VALUES ('00000000-0000-7000-8000-000000000023', '{org}', 'acme/app', "
        "'abc', 'push', now(), '{}'::jsonb, '{}'::jsonb, 'm', 'm/1', '1.0.0', 0.5, 50, "
        "'MEDIUM', '{}'::jsonb, 1, false)"
    )
    conn.exec_driver_sql(
        "INSERT INTO github_installations (installation_id, tenant_id, account_login) "
        f"VALUES (42, '{org}', 'acme')"
    )
    conn.exec_driver_sql(
        "INSERT INTO webhook_deliveries (delivery_id, event) VALUES ('d-1', 'workflow_run')"
    )
    conn.commit()


def test_0005_steps_down_to_0004_with_data_present_and_back_up(pg_url: str) -> None:
    """Downgrading only the M5 revision drops exactly its tables and keeps the rest."""
    cfg = make_config(pg_url)
    command.upgrade(cfg, "head")
    asyncio.run(_run(pg_url, _seed_m5_rows))
    command.downgrade(cfg, "0004_saas")

    tables = asyncio.run(_run(pg_url, _tables))
    assert not tables & M5_TABLES
    assert tables >= M10_TABLES

    def org_count(conn: Connection) -> int:
        return int(conn.exec_driver_sql("SELECT count(*) FROM organizations").scalar_one())

    assert asyncio.run(_run(pg_url, org_count)) == 1

    command.upgrade(cfg, "head")
    assert asyncio.run(_run(pg_url, _tables)) >= M5_TABLES
    diff = asyncio.run(_run(pg_url, _diff))
    assert diff == [], f"models and migrations disagree after a step down/up: {diff}"


def _seed_bad_ci_run(conn: Connection, status: str, finished: str) -> None:
    conn.exec_driver_sql(
        "INSERT INTO organizations (id, name, slug) VALUES "
        "('00000000-0000-7000-8000-000000000031', 'D', 'delta') ON CONFLICT DO NOTHING"
    )
    conn.exec_driver_sql(
        "INSERT INTO ci_runs (id, tenant_id, repository, commit_sha, started_at, finished_at, "
        "status, source, external_id) VALUES (gen_random_uuid(), "
        f"'00000000-0000-7000-8000-000000000031', 'a/b', 'abc', now(), {finished}, "
        f"'{status}', 'api', gen_random_uuid()::text)"
    )
    conn.commit()


@pytest.mark.parametrize(
    ("status", "finished"),
    [
        ("passed", "NULL"),
        ("running", "now()"),
        ("green", "now()"),
        ("passed", "now() - interval '1 day'"),
    ],
)
def test_ci_runs_constraints_reject_inconsistent_rows(
    pg_url: str, status: str, finished: str
) -> None:
    from sqlalchemy.exc import IntegrityError

    command.upgrade(make_config(pg_url), "head")
    with pytest.raises(IntegrityError):
        asyncio.run(_run(pg_url, lambda c: _seed_bad_ci_run(c, status, finished)))

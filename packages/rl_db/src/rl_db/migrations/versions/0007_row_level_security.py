"""Row-level security on every tenant-owned table (ADR-0010 item 3, ADR-0023)

Revision ID: 0007_row_level_security
Revises: 0006_ci_runs_commit_index
Create Date: 2026-10-13

Defence in depth behind the tenant-scoped repositories. Every tenant-scoped repository call
starts with ``SET LOCAL ROLE rl_tenant`` and ``SET LOCAL app.tenant_id = <tenant>``
(:func:`rl_db.repositories._tenancy.bind_tenant`). ``rl_tenant`` is a NOLOGIN role that is
neither a superuser nor a table owner, so the policies below apply to it: it sees and writes
only rows whose ``tenant_id`` equals the bound tenant. When no tenant is bound, it sees no rows.
Cross-tenant maintenance in ``rl_db.admin`` runs as the connecting (owner) role and is not
filtered, as ADR-0010 intends.

The table list is a snapshot. ``test_rls.py`` fails if a later table with a ``tenant_id`` column
does not have RLS enabled.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007_row_level_security"
down_revision: str | None = "0006_ci_runs_commit_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = "rl_tenant"
POLICY = "tenant_isolation"
TENANT_TABLES = (
    "alert_rules",
    "analysis_jobs",
    "api_keys",
    "ci_runs",
    "deployments",
    "environments",
    "github_installations",
    "github_pull_requests",
    "invitations",
    "memberships",
    "notification_log",
    "outbox",
    "projects",
    "regression_analyses",
    "risk_assessments",
    "service_edges",
    "service_minute_rollups",
    "services",
    "spans",
    "subscriptions",
    "usage_counters",
)
# Tables whose tenant_id is nullable by design (rows can exist before a tenant is known) stay
# admin-only and get no policy: webhook_deliveries, stripe_events, audit_log.

_BOUND = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
# A plain literal (no interpolation), kept in step with ROLE by the assert below.
_CREATE_ROLE = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rl_tenant') THEN
        CREATE ROLE rl_tenant NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT;
    END IF;
END
$$
"""
assert f"rolname = '{ROLE}'" in _CREATE_ROLE


def upgrade() -> None:
    op.execute(_CREATE_ROLE)
    # The connecting role must be allowed to switch to rl_tenant (a superuser always is).
    op.execute(f"GRANT {ROLE} TO CURRENT_USER")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {ROLE}")
    op.execute(f"REVOKE ALL ON TABLE alembic_version FROM {ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {ROLE}")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {ROLE}"
    )
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {POLICY} ON {table} TO {ROLE} "
            f"USING (tenant_id = {_BOUND}) WITH CHECK (tenant_id = {_BOUND})"
        )


def downgrade() -> None:
    for table in TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute(
        # ROLE is a module constant, not input
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE USAGE, SELECT ON SEQUENCES FROM {ROLE}"  # noqa: S608  # nosec B608
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {ROLE}"
    )
    op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {ROLE}")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {ROLE}")
    # The role is cluster-wide and other databases (e.g. test databases) may still use it, so it
    # is left in place; it can log in nowhere and owns nothing.

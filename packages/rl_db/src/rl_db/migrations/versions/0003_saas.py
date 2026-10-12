"""SaaS layer (M10): sessions, tokens, invitations, catalog, audit log, alerts, billing, rate limits

Revision ID: 0003_saas
Revises: 0002_deployments_jobs_outbox
Create Date: 2026-10-13

Self-contained: it only adds tables (plus the audit-log immutability triggers) and reuses the
``role`` and ``plan`` enum types created by 0001 without re-creating or dropping them.
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_saas"
down_revision: str | None = "0002_deployments_jobs_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Created by 0001_baseline; referenced here, never created or dropped.
role = postgresql.ENUM("owner", "admin", "member", "viewer", name="role", create_type=False)
plan = postgresql.ENUM("free", "team", "enterprise", name="plan", create_type=False)

JSONB = postgresql.JSONB(astext_type=sa.Text())


def _now() -> sa.TextClause:
    return sa.text("now()")


def _created_updated() -> list[sa.Column[datetime]]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
    ]


AUDIT_IMMUTABLE_FN = """
CREATE FUNCTION audit_log_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only: % is not allowed', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END
$$
"""


def upgrade() -> None:
    # ------------------------------------------------------------------ accounts
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("access_token_hash", sa.String(64), nullable=False),
        sa.Column("access_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("auth_method", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(32), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.CheckConstraint(
            "auth_method IN ('password', 'github')", name=op.f("ck_auth_sessions_auth_method_known")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_auth_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_sessions")),
        sa.UniqueConstraint("access_token_hash", name=op.f("uq_auth_sessions_access_token_hash")),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])

    op.create_table(
        "auth_refresh_tokens",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["auth_sessions.id"],
            name=op.f("fk_auth_refresh_tokens_session_id_auth_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_refresh_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_auth_refresh_tokens_token_hash")),
    )
    op.create_index("ix_auth_refresh_tokens_session_id", "auth_refresh_tokens", ["session_id"])

    op.create_table(
        "user_tokens",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "purpose IN ('verify_email', 'reset_password')",
            name=op.f("ck_user_tokens_purpose_known"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_user_tokens_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_user_tokens_token_hash")),
    )
    op.create_index("ix_user_tokens_user_id_purpose", "user_tokens", ["user_id", "purpose"])

    op.create_table(
        "oauth_states",
        sa.Column("state_hash", sa.String(64), nullable=False),
        sa.Column("code_verifier", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("state_hash", name=op.f("pk_oauth_states")),
    )

    op.create_table(
        "invitations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("role", role, nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("invited_by", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_by", sa.UUID(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["accepted_by"],
            ["users.id"],
            name=op.f("fk_invitations_accepted_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["invited_by"],
            ["users.id"],
            name=op.f("fk_invitations_invited_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["organizations.id"],
            name=op.f("fk_invitations_tenant_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invitations")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_invitations_token_hash")),
    )
    op.create_index(
        "ix_invitations_tenant_id_created_at", "invitations", ["tenant_id", "created_at"]
    )

    # ------------------------------------------------------------------ catalog
    op.create_table(
        "projects",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("github_owner", sa.String(39), nullable=True),
        sa.Column("github_repo", sa.String(100), nullable=True),
        sa.Column("github_installation_id", sa.BigInteger(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        *_created_updated(),
        sa.CheckConstraint(
            "slug ~ '^[a-z0-9][a-z0-9-]{0,63}$'", name=op.f("ck_projects_slug_format")
        ),
        sa.CheckConstraint(
            "(github_owner IS NULL) = (github_repo IS NULL)",
            name=op.f("ck_projects_github_repo_complete"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_projects_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["organizations.id"],
            name=op.f("fk_projects_tenant_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_projects")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_projects_tenant_id_id")),
        sa.UniqueConstraint("tenant_id", "slug", name=op.f("uq_projects_tenant_id_slug")),
    )

    op.create_table(
        "services",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        *_created_updated(),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name=op.f("fk_services_tenant_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_services")),
        sa.UniqueConstraint("tenant_id", "name", name=op.f("uq_services_tenant_id_name")),
    )
    op.create_index("ix_services_tenant_id_project_id", "services", ["tenant_id", "project_id"])

    op.create_table(
        "environments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        *_created_updated(),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name=op.f("fk_environments_tenant_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_environments")),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "name",
            name=op.f("uq_environments_tenant_id_project_id_name"),
        ),
    )

    # ------------------------------------------------------------------ audit log
    op.create_table(
        "audit_log",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=True),
        sa.Column("actor_type", sa.String(16), nullable=False),
        sa.Column("actor_user_id", sa.UUID(), nullable=True),
        sa.Column("actor_api_key_id", sa.UUID(), nullable=True),
        sa.Column("actor_email", sa.String(320), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(64), nullable=True),
        sa.Column("target_id", sa.String(200), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.Column("metadata", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.CheckConstraint(
            "actor_type IN ('user', 'api_key', 'system', 'anonymous')",
            name=op.f("ck_audit_log_actor_type_known"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(metadata) = 'object'", name=op.f("ck_audit_log_metadata_is_object")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index(
        "ix_audit_log_tenant_created_at_id",
        "audit_log",
        ["tenant_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_audit_log_tenant_action_created_at", "audit_log", ["tenant_id", "action", "created_at"]
    )
    # Append-only at the database level: ordinary DML cannot rewrite or erase history.
    op.execute(AUDIT_IMMUTABLE_FN)
    op.execute(
        "CREATE TRIGGER audit_log_no_update_delete BEFORE UPDATE OR DELETE ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION audit_log_immutable()"
    )
    op.execute(
        "CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log "
        "FOR EACH STATEMENT EXECUTE FUNCTION audit_log_immutable()"
    )

    # ------------------------------------------------------------------ alerts
    op.create_table(
        "alert_rules",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("on_regression", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("regression_min_band", sa.String(16), server_default="HIGH", nullable=False),
        sa.Column("on_risk", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("risk_min_band", sa.String(16), server_default="HIGH", nullable=False),
        sa.Column("slack_webhook_ciphertext", sa.Text(), nullable=True),
        sa.Column("slack_webhook_hint", sa.String(80), nullable=True),
        sa.Column(
            "email_recipients",
            postgresql.ARRAY(sa.String(320)),
            server_default=sa.text("'{}'::varchar[]"),
            nullable=False,
        ),
        sa.Column("created_by", sa.UUID(), nullable=True),
        *_created_updated(),
        sa.CheckConstraint(
            "regression_min_band IN ('HIGH', 'CRITICAL')",
            name=op.f("ck_alert_rules_regression_band"),
        ),
        sa.CheckConstraint(
            "risk_min_band IN ('HIGH', 'CRITICAL')", name=op.f("ck_alert_rules_risk_band")
        ),
        sa.CheckConstraint(
            "cardinality(email_recipients) <= 20", name=op.f("ck_alert_rules_recipients_max")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_alert_rules_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name=op.f("fk_alert_rules_tenant_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert_rules")),
    )
    op.create_index(
        "ix_alert_rules_tenant_id_project_id", "alert_rules", ["tenant_id", "project_id"]
    )

    op.create_table(
        "notification_log",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("alert_rule_id", sa.UUID(), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("subject_id", sa.UUID(), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="6", nullable=False),
        sa.Column(
            "next_attempt_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False
        ),
        sa.Column("locked_by", sa.String(200), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("payload", JSONB, nullable=False),
        *_created_updated(),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "channel IN ('slack', 'email')", name=op.f("ck_notification_log_channel_known")
        ),
        sa.CheckConstraint(
            "kind IN ('regression', 'risk')", name=op.f("ck_notification_log_kind_known")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'failed', 'dead', 'skipped')",
            name=op.f("ck_notification_log_status_known"),
        ),
        sa.ForeignKeyConstraint(
            ["alert_rule_id"],
            ["alert_rules.id"],
            name=op.f("fk_notification_log_alert_rule_id_alert_rules"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["organizations.id"],
            name=op.f("fk_notification_log_tenant_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_log")),
        sa.UniqueConstraint(
            "tenant_id",
            "alert_rule_id",
            "subject_id",
            "kind",
            "channel",
            name="uq_notification_log_idempotency",
        ),
    )
    op.create_index(
        "ix_notification_log_status_next_attempt_at",
        "notification_log",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_notification_log_tenant_id_created_at", "notification_log", ["tenant_id", "created_at"]
    )

    # ------------------------------------------------------------------ billing
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("stripe_customer_id", sa.String(255), nullable=False),
        sa.Column("stripe_subscription_id", sa.String(255), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("plan", plan, nullable=False),
        sa.Column("price_id", sa.String(255), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_at_period_end", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("payment_failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
        *_created_updated(),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["organizations.id"],
            name=op.f("fk_subscriptions_tenant_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subscriptions")),
        sa.UniqueConstraint("stripe_customer_id", name=op.f("uq_subscriptions_stripe_customer_id")),
        sa.UniqueConstraint(
            "stripe_subscription_id", name=op.f("uq_subscriptions_stripe_subscription_id")
        ),
        sa.UniqueConstraint("tenant_id", name=op.f("uq_subscriptions_tenant_id")),
    )

    op.create_table(
        "stripe_events",
        sa.Column("id", sa.String(255), nullable=False),
        sa.Column("type", sa.String(100), nullable=False),
        sa.Column("stripe_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stripe_events")),
    )

    op.create_table(
        "usage_counters",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("metric", sa.String(64), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("value", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["organizations.id"],
            name=op.f("fk_usage_counters_tenant_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "metric", "period_start", name=op.f("pk_usage_counters")
        ),
    )

    op.create_table(
        "rate_limit_buckets",
        sa.Column("bucket_key", sa.String(255), nullable=False),
        sa.Column("tokens", sa.Double(), nullable=False),
        sa.Column("allowed", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("bucket_key", name=op.f("pk_rate_limit_buckets")),
    )
    op.create_index("ix_rate_limit_buckets_updated_at", "rate_limit_buckets", ["updated_at"])


def downgrade() -> None:
    op.drop_index("ix_rate_limit_buckets_updated_at", table_name="rate_limit_buckets")
    op.drop_table("rate_limit_buckets")
    op.drop_table("usage_counters")
    op.drop_table("stripe_events")
    op.drop_table("subscriptions")
    op.drop_index("ix_notification_log_tenant_id_created_at", table_name="notification_log")
    op.drop_index("ix_notification_log_status_next_attempt_at", table_name="notification_log")
    op.drop_table("notification_log")
    op.drop_index("ix_alert_rules_tenant_id_project_id", table_name="alert_rules")
    op.drop_table("alert_rules")
    # DROP TABLE is DDL, not DML: the immutability triggers do not block it.
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log")
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_update_delete ON audit_log")
    op.drop_index("ix_audit_log_tenant_action_created_at", table_name="audit_log")
    op.drop_index("ix_audit_log_tenant_created_at_id", table_name="audit_log")
    op.drop_table("audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_immutable()")
    op.drop_table("environments")
    op.drop_index("ix_services_tenant_id_project_id", table_name="services")
    op.drop_table("services")
    op.drop_table("projects")
    op.drop_index("ix_invitations_tenant_id_created_at", table_name="invitations")
    op.drop_table("invitations")
    op.drop_table("oauth_states")
    op.drop_index("ix_user_tokens_user_id_purpose", table_name="user_tokens")
    op.drop_table("user_tokens")
    op.drop_index("ix_auth_refresh_tokens_session_id", table_name="auth_refresh_tokens")
    op.drop_table("auth_refresh_tokens")
    op.drop_index("ix_auth_sessions_user_id", table_name="auth_sessions")
    op.drop_table("auth_sessions")

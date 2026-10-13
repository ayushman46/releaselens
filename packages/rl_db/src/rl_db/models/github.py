"""GitHub App integration (M5, BLOCKERS.md B1).

* ``github_installations``: which tenant an App installation belongs to. A webhook names only
  its installation, so this table is how a delivery *finds out* its tenant (the lookup lives in
  :mod:`rl_db.admin.github`). An installation belongs to at most one tenant (primary key).
  Rows are created by an operator (``python -m rl_api.admin link-github-installation``), never
  from a webhook: anyone can install a public App, so an installation id proves nothing about
  which ReleaseLens organization it belongs to.
* ``webhook_deliveries``: one row per accepted ``X-GitHub-Delivery`` id. GitHub redelivers on
  timeouts and on manual "Redeliver", so processing is deduplicated on this key.
* ``github_pull_requests``: the latest known state of a pull request (no titles, bodies or
  authors are stored: the model never uses them).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from rl_core.events import deployment as limits
from rl_db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

MAX_GITHUB_LOGIN_LEN = 39
MAX_DELIVERY_ID_LEN = 100
MAX_EVENT_NAME_LEN = 64


class GithubInstallation(TimestampMixin, Base):
    __tablename__ = "github_installations"

    installation_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # The GitHub account (organization or user) the App is installed on, lower-cased.
    account_login: Mapped[str] = mapped_column(String(MAX_GITHUB_LOGIN_LEN), nullable=False)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_github_installations_tenant_account", "tenant_id", "account_login"),
        CheckConstraint("installation_id > 0", name="installation_id_positive"),
        CheckConstraint("account_login = lower(account_login)", name="account_login_lower"),
    )


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    delivery_id: Mapped[str] = mapped_column(String(MAX_DELIVERY_ID_LEN), primary_key=True)
    event: Mapped[str] = mapped_column(String(MAX_EVENT_NAME_LEN), nullable=False)
    installation_id: Mapped[int | None] = mapped_column(BigInteger)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    outcome: Mapped[str | None] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_webhook_deliveries_received_at", "received_at"),
        Index("ix_webhook_deliveries_tenant_received_at", "tenant_id", "received_at"),
    )


class GithubPullRequest(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "github_pull_requests"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    repository: Mapped[str] = mapped_column(String(limits.MAX_REPOSITORY_LEN), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    head_sha: Mapped[str] = mapped_column(String(limits.MAX_COMMIT_SHA_LEN), nullable=False)
    base_sha: Mapped[str | None] = mapped_column(String(limits.MAX_COMMIT_SHA_LEN))
    base_ref: Mapped[str | None] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    merged: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    draft: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    github_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "repository", "number"),
        CheckConstraint("number > 0", name="number_positive"),
        CheckConstraint("state IN ('open', 'closed')", name="state_known"),
    )

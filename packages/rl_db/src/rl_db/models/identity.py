"""Identity & tenancy tables (M1 baseline; invitations added in M10).

Tenant-owned tables carry ``tenant_id NOT NULL`` referencing ``organizations.id`` and every
index on them leads with ``tenant_id`` (ADR-0010).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from rl_db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class Plan(StrEnum):
    FREE = "free"
    TEAM = "team"
    ENTERPRISE = "enterprise"


class Organization(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A tenant. ``organizations.id`` *is* the ``tenant_id`` used everywhere else."""

    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    plan: Mapped[Plan] = mapped_column(
        Enum(Plan, name="plan", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        server_default=Plan.FREE.value,
    )
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    __table_args__ = (CheckConstraint("slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'", name="slug_format"),)


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A person. Users are global; access to tenants goes through memberships."""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200))
    password_hash: Mapped[str | None] = mapped_column(Text)  # Argon2id; null for OAuth-only users
    github_user_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("uq_users_email_lower", func.lower(email), unique=True),)


class Membership(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "memberships"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[Role] = mapped_column(
        Enum(Role, name="role", values_callable=lambda e: [m.value for m in e]), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id"),
        Index("ix_memberships_user_id", "user_id"),
    )


class ApiKey(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Tenant API key. Only a hash of the secret is stored; ``prefix`` is shown in the UI."""

    __tablename__ = "api_keys"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    prefix: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    secret_hash: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_api_keys_tenant_id_created_at", "tenant_id", "created_at"),
        CheckConstraint(
            "scopes <@ ARRAY['deployments:write','telemetry:write','risk:write','read']::varchar[]",
            name="scopes_known",
        ),
    )


class Invitation(UUIDPrimaryKeyMixin, Base):
    """An invitation to join an organization, bound to one email address.

    Only ``sha256(token)`` is stored; the token itself is e-mailed once. It is single use
    (``accepted_at``), expires (``expires_at``, 7 days) and can be withdrawn (``revoked_at``).
    """

    __tablename__ = "invitations"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[Role] = mapped_column(
        Enum(Role, name="role", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_invitations_tenant_id_created_at", "tenant_id", "created_at"),)

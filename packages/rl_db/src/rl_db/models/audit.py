"""Append-only audit log (M10; docs/security/AUTH.md §8).

Rows can only be inserted. Migration ``0003_saas`` installs triggers that raise on
``UPDATE``, ``DELETE`` and ``TRUNCATE``, so not even a bug (or an attacker holding the
application's database role) can rewrite history through ordinary DML.

``tenant_id`` is nullable and has **no** foreign key on purpose:

* sign-in events (success and failure) belong to a user, not an organization, and are
  written with ``tenant_id`` NULL;
* the log must outlive the organization it describes (``org.deleted`` is itself an entry),
  and a cascading delete would be blocked by the trigger anyway.

The actor columns have no foreign keys for the same reason; ``actor_email`` keeps the
actor readable after the user is gone.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from ipaddress import IPv4Address, IPv6Address
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Index, String, func, text
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from rl_db.base import Base, UUIDPrimaryKeyMixin

ACTOR_TYPES = ("user", "api_key", "system", "anonymous")


class AuditLogEntry(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "audit_log"

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actor_api_key_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actor_email: Mapped[str | None] = mapped_column(String(320))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(64))
    target_id: Mapped[str | None] = mapped_column(String(200))
    # asyncpg returns ipaddress objects for INET; str() gives the canonical text form.
    ip: Mapped[IPv4Address | IPv6Address | str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(512))
    # ``metadata`` is reserved by SQLAlchemy's declarative base, hence the attribute name.
    details: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # The org audit view: newest first, keyset-paginated on (created_at, id).
        Index(
            "ix_audit_log_tenant_created_at_id",
            "tenant_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
        Index("ix_audit_log_tenant_action_created_at", "tenant_id", "action", "created_at"),
        CheckConstraint(
            "actor_type IN ('user', 'api_key', 'system', 'anonymous')", name="actor_type_known"
        ),
        CheckConstraint("jsonb_typeof(metadata) = 'object'", name="metadata_is_object"),
    )

"""Projects, services and environments (M10).

A **project** is usually one repository (``github_owner``/``github_repo``). It owns
**services** (the ``service.name`` its telemetry and deployment events use) and
**environments** (``production``, ``staging``, …) shared by its services.

Service names are unique per *tenant*, not per project: deployment events and spans carry
only ``service``, so a name must map to exactly one project.
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from rl_core.events import deployment as limits
from rl_db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

MAX_PROJECT_NAME_LEN = 200
MAX_PROJECT_SLUG_LEN = 64
MAX_GITHUB_OWNER_LEN = 39  # GitHub's own limit
MAX_GITHUB_REPO_LEN = 100
MAX_DESCRIPTION_LEN = 500


class Project(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "projects"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(MAX_PROJECT_NAME_LEN), nullable=False)
    slug: Mapped[str] = mapped_column(String(MAX_PROJECT_SLUG_LEN), nullable=False)
    github_owner: Mapped[str | None] = mapped_column(String(MAX_GITHUB_OWNER_LEN))
    github_repo: Mapped[str | None] = mapped_column(String(MAX_GITHUB_REPO_LEN))
    # Set when the ReleaseLens GitHub App is installed on the repository (BLOCKERS.md B1).
    github_installation_id: Mapped[int | None] = mapped_column(BigInteger)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "slug"),
        # Target of the composite foreign keys below: children stay in their project's tenant.
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint("slug ~ '^[a-z0-9][a-z0-9-]{0,63}$'", name="slug_format"),
        CheckConstraint(
            "(github_owner IS NULL) = (github_repo IS NULL)", name="github_repo_complete"
        ),
    )


class Service(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "services"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(limits.MAX_SERVICE_LEN), nullable=False)
    description: Mapped[str | None] = mapped_column(String(MAX_DESCRIPTION_LEN))

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("tenant_id", "name"),
        Index("ix_services_tenant_id_project_id", "tenant_id", "project_id"),
    )


class Environment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "environments"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(limits.MAX_ENVIRONMENT_LEN), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("tenant_id", "project_id", "name"),
    )

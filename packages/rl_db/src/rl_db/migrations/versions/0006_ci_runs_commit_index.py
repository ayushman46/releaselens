"""ci_runs: index for the CI outcome of one commit

Revision ID: 0006_ci_runs_commit_index
Revises: 0005_risk
Create Date: 2026-10-13

``GET /api/v1/ci-runs?commit_sha=`` looks up a commit's runs without a repository (a deployment
event carries the commit but not always the repository). The existing ci_runs indexes all lead
with ``(tenant_id, repository)``, so that lookup would scan the tenant's whole build history.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_ci_runs_commit_index"
down_revision: str | None = "0005_risk"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_ci_runs_tenant_commit_sha",
        "ci_runs",
        ["tenant_id", "commit_sha", sa.text("started_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_ci_runs_tenant_commit_sha", table_name="ci_runs")

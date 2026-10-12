"""Tenant-scoped repositories (ADR-0010).

Every function takes a :class:`rl_core.tenancy.TenantContext` and filters or stamps rows with
its ``tenant_id``. A missing tenant filter is the bug class these signatures exist to make
impossible. Queries that must span tenants live in :mod:`rl_db.admin`, and nowhere else.

Functions never commit. The caller owns the transaction, so a domain write and its outbox
row (:func:`add_outbox_event`) commit together or not at all.
"""

from rl_db.repositories.api_keys import insert_api_key, list_api_keys, revoke_api_key
from rl_db.repositories.deployments import (
    DEFAULT_POST_WINDOW_END,
    DeploymentCursor,
    RecordedDeployment,
    analysis_schedule,
    get_deployment,
    list_analysis_jobs,
    list_deployments,
    record_deployment,
)
from rl_db.repositories.outbox import add_outbox_event

__all__ = [
    "DEFAULT_POST_WINDOW_END",
    "DeploymentCursor",
    "RecordedDeployment",
    "add_outbox_event",
    "analysis_schedule",
    "get_deployment",
    "insert_api_key",
    "list_analysis_jobs",
    "list_api_keys",
    "list_deployments",
    "record_deployment",
    "revoke_api_key",
]

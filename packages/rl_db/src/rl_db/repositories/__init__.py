"""Tenant-scoped repositories (ADR-0010).

Every function takes a :class:`rl_core.tenancy.TenantContext` and filters or stamps rows with
its ``tenant_id``. A missing tenant filter is the bug class these signatures exist to make
impossible. Queries that must span tenants live in :mod:`rl_db.admin`, and nowhere else.

Functions never commit. The caller owns the transaction, so a domain write and its outbox
row (:func:`add_outbox_event`) commit together or not at all.
"""

from rl_db.repositories.analyses import (
    AnalysisWrite,
    RegressionCursor,
    SpanLoad,
    latest_analysis_windows,
    latest_environment_analysis,
    list_analyses,
    list_regressions,
    load_analysis_spans,
    upsert_analysis,
)
from rl_db.repositories.api_keys import (
    get_api_key,
    insert_api_key,
    list_api_keys,
    revoke_api_key,
)
from rl_db.repositories.deployments import (
    DEFAULT_POST_WINDOW_END,
    DeploymentCursor,
    LatestAnalysis,
    RecordedDeployment,
    analysis_schedule,
    get_deployment,
    get_deployment_with_latest_analysis,
    list_analysis_jobs,
    list_deployments,
    list_deployments_with_latest_analysis,
    record_deployment,
)
from rl_db.repositories.outbox import add_outbox_event
from rl_db.repositories.telemetry import SpanWriteResult, record_spans

__all__ = [
    "DEFAULT_POST_WINDOW_END",
    "AnalysisWrite",
    "DeploymentCursor",
    "LatestAnalysis",
    "RecordedDeployment",
    "RegressionCursor",
    "SpanLoad",
    "SpanWriteResult",
    "add_outbox_event",
    "analysis_schedule",
    "get_api_key",
    "get_deployment",
    "get_deployment_with_latest_analysis",
    "insert_api_key",
    "latest_analysis_windows",
    "latest_environment_analysis",
    "list_analyses",
    "list_analysis_jobs",
    "list_api_keys",
    "list_deployments",
    "list_deployments_with_latest_analysis",
    "list_regressions",
    "load_analysis_spans",
    "record_deployment",
    "record_spans",
    "revoke_api_key",
    "upsert_analysis",
]

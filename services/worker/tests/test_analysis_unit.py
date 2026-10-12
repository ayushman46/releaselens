"""Analysis handler configuration, as_of choice and the analysis.results summary (no I/O)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import tracegen as tg
from pydantic import ValidationError

from rl_core.events.analysis import encode_analysis, make_analysis_envelope, parse_analysis
from rl_core.regression.analysis import analyse_deployment
from rl_core.regression.windows import WindowConfig
from rl_db.models import JobKind
from rl_db.repositories import analysis_schedule
from rl_worker.analysis import (
    AnalysisJobHandler,
    AnalysisSettings,
    deployed_service_status,
    summarize,
)
from rl_worker.settings import WorkerSettings

T = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def test_profiles_resolve_per_environment_and_must_fit_the_schedule() -> None:
    demo = WindowConfig(baseline_minutes=10, warmup_minutes=1, post_end_minutes=8)
    settings = AnalysisSettings(per_environment={"demo": demo})
    assert settings.config_for("demo") == ("demo", demo)
    assert settings.config_for("production") == ("default", WindowConfig())
    with pytest.raises(ValueError, match="after the final analysis"):
        AnalysisSettings(per_environment={"slow": WindowConfig(post_end_minutes=20)})
    with pytest.raises(ValueError, match="max_spans"):
        AnalysisSettings(max_spans=0)


def test_as_of_is_the_scheduled_time_for_early_kinds_and_none_for_final() -> None:
    handler = AnalysisJobHandler(None, AnalysisSettings())  # type: ignore[arg-type]
    late = T + timedelta(hours=2)
    assert handler.as_of(JobKind.EARLY_5M, T, late) == T + timedelta(minutes=5)
    assert handler.as_of(JobKind.EARLY_10M, T, late) == T + timedelta(minutes=10)
    # Never in the future of the actual run (clock skew, early claim) nor before the deploy.
    assert handler.as_of(JobKind.EARLY_10M, T, T + timedelta(minutes=7)) == T + timedelta(minutes=7)
    assert handler.as_of(JobKind.EARLY_5M, T, T - timedelta(minutes=1)) == T
    assert handler.as_of(JobKind.FINAL, T, late) is None


def test_worker_settings_window_profiles() -> None:
    settings = WorkerSettings()
    assert settings.window_profiles()["demo"] == WindowConfig(
        baseline_minutes=10, warmup_minutes=1, post_end_minutes=8
    )
    with pytest.raises(ValidationError, match="after the final analysis"):
        WorkerSettings(analysis_windows={"x": {"post_end_minutes": 20}})
    with pytest.raises(ValidationError):
        WorkerSettings(analysis_windows={"x": {"warmup_minutes": 9, "post_end_minutes": 8}})
    assert (
        WorkerSettings(
            analysis_windows={"x": {"post_end_minutes": 20}}, analysis_post_window_end_s=1200
        )
        .window_profiles()["x"]
        .post_end_minutes
        == 20
    )
    assert dict(analysis_schedule(timedelta(seconds=1200)))[JobKind.FINAL] == timedelta(minutes=20)


def _deployment(**kw: Any) -> Any:
    fields: dict[str, Any] = {
        "id": uuid.uuid4(),
        "service": "payment",
        "environment": "demo",
        "version": "v2.18",
        "previous_version": "v2.17",
    }
    fields.update(kw)
    return SimpleNamespace(**fields)


def test_summary_follows_the_analysis_results_contract() -> None:
    scenario = tg.payment_regression()
    analysis = analyse_deployment(tg.generate(scenario, 0), scenario.deployment(), seed=0)
    deployment = _deployment()
    summary = summarize(
        analysis,
        deployment,  # type: ignore[arg-type]
        analysis_id=uuid.uuid4(),
        kind=JobKind.FINAL,
        completed_at=T,
    )
    assert summary.status == "REGRESSED"  # the deployed service's own status
    assert (summary.probable_origin, summary.confidence) == ("payment", "HIGH")
    assert summary.affected_services == ["orders", "gateway"]
    assert summary.severity_band in {"HIGH", "CRITICAL"}
    tenant = uuid.uuid4()
    envelope = parse_analysis(
        encode_analysis(make_analysis_envelope(tenant_id=tenant, summary=summary))
    )
    assert envelope.payload == summary
    assert envelope.tenant_id == tenant

    # A deployed service the analysis never saw is reported as not assessed.
    assert deployed_service_status(analysis, "not-there").value == "INSUFFICIENT_DATA"

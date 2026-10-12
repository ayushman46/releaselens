"""Job handler registry and runner configuration (no database)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from rl_db.admin.jobs import ClaimedJob
from rl_db.models import JobKind
from rl_worker.jobs import HandlerRegistry, JobRunner, default_registry


async def _noop(_job: ClaimedJob) -> None:
    return None


def test_default_registry_runs_the_analysis_for_every_kind() -> None:
    registry = default_registry(_noop)
    assert registry.kinds == frozenset(JobKind)
    assert all(registry.get(kind) is _noop for kind in JobKind)


def test_registry_rejects_double_registration() -> None:
    registry = HandlerRegistry()
    registry.register(JobKind.FINAL, _noop)
    assert registry.kinds == {JobKind.FINAL}
    assert registry.get(JobKind.FINAL) is _noop
    assert registry.get(JobKind.EARLY_5M) is None
    with pytest.raises(ValueError, match="already registered"):
        registry.register(JobKind.FINAL, _noop)


def test_job_timeout_must_be_shorter_than_lease() -> None:
    with pytest.raises(ValueError, match="shorter than the lease"):
        JobRunner(
            None,  # type: ignore[arg-type]
            HandlerRegistry(),
            worker_id="w",
            lease=timedelta(minutes=1),
            job_timeout=timedelta(minutes=1),
        )

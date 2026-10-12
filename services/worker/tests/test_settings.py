"""Worker settings validation that must fail at startup, not on first use."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from rl_worker.settings import WorkerSettings


def test_job_timeout_must_fit_inside_the_lease() -> None:
    """A job that may run longer than its lease could be re-claimed and run twice."""
    with pytest.raises(ValidationError, match="RL_JOB_TIMEOUT_S"):
        WorkerSettings(job_lease_s=60, job_timeout_s=60)
    assert WorkerSettings(job_lease_s=60, job_timeout_s=59).job_timeout_s == 59


def test_defaults_are_valid() -> None:
    settings = WorkerSettings()
    assert settings.job_timeout_s < settings.job_lease_s

"""Analysis windows and every threshold the regression engine applies.

For a deployment at ``T`` (spec M8)::

    baseline = [T - baseline_minutes, T)              (end exclusive)
    warm-up  = [T, T + warmup_minutes)                 (excluded from the gates; used for onset)
    post     = [T + warmup_minutes, T + post_end_minutes]   (end INCLUSIVE, as specified)

A span belongs to a window when its **start** time does. The whole :class:`WindowConfig` and the
resolved :class:`AnalysisWindows` are stored with every analysis, so a result can always be
re-derived and explained.

**Partial post windows.** Analysis jobs run at T+5, T+10 and T+15 minutes (ADR-0011). Passing
``as_of`` truncates the post window to ``[post_start, min(post_end, as_of)]`` so that throughput is
divided by the minutes actually observed; without it a run at T+5 would see "3 of 13 minutes" of
traffic and report a spurious throughput drop.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rl_core.telemetry.model import NS_PER_S, datetime_to_ns

#: Percentile that severity v1 and the onset estimate are defined on.
SEVERITY_PERCENTILE = 0.95

RELAXED_FACTOR = 0.5


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetimes must be timezone-aware (UTC)")
    return value.astimezone(UTC)


class AnalysisWindows(BaseModel):
    """Resolved windows for one deployment (all instants are UTC)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    deployed_at: datetime
    baseline_start: datetime
    baseline_end: datetime  # exclusive
    post_start: datetime
    post_end: datetime  # inclusive
    effective_post_end: datetime  # inclusive; min(post_end, as_of)
    as_of: datetime | None

    @property
    def deployed_at_ns(self) -> int:
        return datetime_to_ns(self.deployed_at)

    @property
    def baseline_start_ns(self) -> int:
        return datetime_to_ns(self.baseline_start)

    @property
    def baseline_end_ns(self) -> int:
        return datetime_to_ns(self.baseline_end)

    @property
    def post_start_ns(self) -> int:
        return datetime_to_ns(self.post_start)

    @property
    def effective_post_end_ns(self) -> int:
        return datetime_to_ns(self.effective_post_end)

    @property
    def baseline_minutes(self) -> float:
        return (self.baseline_end_ns - self.baseline_start_ns) / (60 * NS_PER_S)

    @property
    def post_minutes(self) -> float:
        """Observed post-window length; 0 when ``as_of`` precedes the post window."""
        return max(0, self.effective_post_end_ns - self.post_start_ns) / (60 * NS_PER_S)

    def in_baseline(self, start_ns: int) -> bool:
        return self.baseline_start_ns <= start_ns < self.baseline_end_ns

    def in_post(self, start_ns: int) -> bool:
        return self.post_start_ns <= start_ns <= self.effective_post_end_ns


class WindowConfig(BaseModel):
    """All windows and gate thresholds (spec M8 defaults). Immutable and JSON-serialisable.

    Infinite thresholds are rejected: they are stored with every analysis (JSON has no inf) and
    the error gates compare thresholds as exact decimals.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    name: str = "strict"

    baseline_minutes: int = Field(default=30, gt=0)
    warmup_minutes: int = Field(default=2, ge=0)
    post_end_minutes: int = Field(default=15, gt=0)

    min_requests: int = Field(default=200, ge=1)

    latency_rel_threshold: float = Field(default=0.25, ge=0)
    latency_abs_threshold_ms: float = Field(default=20.0, ge=0)
    bootstrap_resamples: int = Field(default=1000, ge=1000)
    bootstrap_confidence: float = Field(default=0.95, gt=0, lt=1)
    bootstrap_seed: int = Field(default=0, ge=0)

    error_p_threshold: float = Field(default=0.01, gt=0, lt=1)
    error_abs_threshold_pp: float = Field(default=0.5, ge=0)
    error_rel_threshold: float = Field(default=0.5, ge=0)

    throughput_drop_threshold: float = Field(default=0.30, gt=0, lt=1)

    percentiles: tuple[float, ...] = (0.5, 0.95, 0.99)

    onset_bucket_seconds: int = Field(default=60, gt=0)
    onset_min_samples: int = Field(default=20, ge=1)

    @field_validator("percentiles")
    @classmethod
    def _check_percentiles(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if not value:
            raise ValueError("at least one percentile is required")
        if any(not 0 < q < 1 for q in value):
            raise ValueError("percentiles must be within (0, 1)")
        if len(set(value)) != len(value):
            raise ValueError("percentiles must be unique")
        if SEVERITY_PERCENTILE not in value:
            # Severity v1 and the onset estimate are defined on p95; without it they are undefined.
            raise ValueError("percentiles must include 0.95 (severity v1 and onset use p95)")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def _check_windows(self) -> Self:
        if self.post_end_minutes <= self.warmup_minutes:
            raise ValueError("post_end_minutes must be greater than warmup_minutes")
        return self

    def windows(self, deployed_at: datetime, as_of: datetime | None = None) -> AnalysisWindows:
        t = _utc(deployed_at)
        post_end = t + timedelta(minutes=self.post_end_minutes)
        cut = None if as_of is None else _utc(as_of)
        if cut is not None and cut < t:
            # Before T the baseline itself is incomplete and there is nothing to analyse.
            raise ValueError("as_of must not precede the deployment")
        return AnalysisWindows(
            deployed_at=t,
            baseline_start=t - timedelta(minutes=self.baseline_minutes),
            baseline_end=t,
            post_start=t + timedelta(minutes=self.warmup_minutes),
            post_end=post_end,
            effective_post_end=post_end if cut is None else min(post_end, cut),
            as_of=cut,
        )

    def relaxed(self, factor: float = RELAXED_FACTOR) -> WindowConfig:
        """Variant for blast-radius neighbours of an already identified probable origin.

        Only the **effect-size** gates are scaled by ``factor`` (default ½): latency relative and
        absolute change, error-rate absolute and relative increase. The **evidence** gates stay
        exactly as strict — ``min_requests``, the bootstrap confidence and the z-test p threshold —
        because the relaxation is justified by prior evidence (a confirmed origin next door) that
        makes smaller effects plausible, not by any reason to accept noise as a signal.
        """
        if not 0 < factor <= 1:
            raise ValueError("factor must be in (0, 1]")
        return self.model_copy(
            update={
                "name": "relaxed",
                "latency_rel_threshold": self.latency_rel_threshold * factor,
                "latency_abs_threshold_ms": self.latency_abs_threshold_ms * factor,
                "error_abs_threshold_pp": self.error_abs_threshold_pp * factor,
                "error_rel_threshold": self.error_rel_threshold * factor,
            }
        )

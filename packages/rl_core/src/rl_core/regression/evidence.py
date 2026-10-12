"""Inputs and JSON-serialisable evidence models of the regression engine.

Every number a user sees about a regression is one of these fields, so a verdict can always be
traced back to the measured values, the thresholds and the named gates that produced it.

Conventions:

* ``*_rate`` fields are fractions in ``[0, 1]``; ``*_pp`` fields are percentage points.
* Undefined quantities are ``None``, never NaN/inf (JSON has neither). In particular a relative
  change against a zero baseline is ``None`` and the gate's ``note`` says how it was decided.
* :class:`ServiceStatus` (what the gates concluded) and :class:`SeverityBand` (a deterministic
  score bucket) are deliberately separate enums. They usually agree but need not: a throughput-only
  WARNING has score 0 (band HEALTHY), and a regression confined to p99 or to a small p95 increase
  can be REGRESSED with a low band. Severity is **not** a probability.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ServiceStatus(StrEnum):
    HEALTHY = "HEALTHY"
    WARNING = "WARNING"  # throughput drop only; never escalates to REGRESSED on its own
    REGRESSED = "REGRESSED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"  # sample gate failed; no other gate evaluated


class SeverityBand(StrEnum):
    HEALTHY = "HEALTHY"  # score < 15
    WARNING = "WARNING"  # 15 <= score < 40
    HIGH = "HIGH"  # 40 <= score < 70
    CRITICAL = "CRITICAL"  # score >= 70


class DeploymentRef(_Model):
    """The deployment being analysed (subset of the deployment event)."""

    service: str
    environment: str | None = None
    version: str | None = None
    previous_version: str | None = None
    deployed_at: datetime
    tenant_id: str | None = None  # when set, every span must belong to this tenant

    @field_validator("deployed_at")
    @classmethod
    def _aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("deployed_at must be timezone-aware")
        return value.astimezone(UTC)


Comparator = Literal[">=", ">", "<", "<="]


def percentile_label(q: float) -> str:
    """``0.95`` → ``"p95"``, ``0.999`` → ``"p99.9"``.

    Rounding to a whole percent would give 0.995 and 0.999 the same label (``"p100"``).
    """
    return f"p{round(q * 100, 4):g}"


class Gate(_Model):
    """One named check. ``passed`` is the decision; the rest is the evidence for it."""

    name: str
    passed: bool
    observed: float | None
    threshold: float
    op: Comparator
    note: str | None = None


class SampleGate(_Model):
    baseline_requests: int
    post_requests: int
    min_requests: int
    passed: bool


class LatencyEvidence(_Model):
    """Gates on one percentile of a latency distribution (total latency or service self-time)."""

    metric: Literal["latency", "self_time"]
    percentile: float
    baseline_ms: float
    post_ms: float
    abs_delta_ms: float
    rel_delta: float | None  # None when the baseline percentile is 0
    ci_low_ms: float
    ci_high_ms: float
    ci_confidence: float
    bootstrap_resamples: int
    bootstrap_seed: int
    n_baseline: int
    n_post: int
    gates: tuple[Gate, ...]
    regressed: bool

    @property
    def label(self) -> str:
        return f"{self.metric}_{percentile_label(self.percentile)}"


class ErrorRateEvidence(_Model):
    """Gates on an error rate: all errors (``error_rate``) or own errors (``own_error_rate``)."""

    metric: Literal["error_rate", "own_error_rate"]
    baseline_errors: int
    baseline_requests: int
    post_errors: int
    post_requests: int
    baseline_rate: float
    post_rate: float
    abs_delta_pp: float
    rel_delta: float | None  # None when the baseline rate is 0 (unbounded relative increase)
    z: float
    p_value: float  # one-sided pooled two-proportion z-test
    # One-sided Fisher exact test; the engine always sets it (None only on hand-built evidence).
    exact_p_value: float | None = None
    gates: tuple[Gate, ...]
    regressed: bool


class ThroughputEvidence(_Model):
    baseline_rpm: float | None
    post_rpm: float | None
    rel_change: float | None  # (post - baseline) / baseline
    gate: Gate
    dropped: bool


class OnsetBucket(_Model):
    start: datetime
    samples: int
    p95_ms: float | None  # None when the bucket has fewer than the minimum samples
    exceeds: bool


class OnsetEvidence(_Model):
    """First minute bucket (from T, warm-up included) whose p95 exceeds the baseline p95 CI."""

    onset_at: datetime | None
    minutes_after_deploy: int | None
    baseline_p95_ms: float
    baseline_p95_ci_high_ms: float
    ci_confidence: float
    bootstrap_resamples: int
    bootstrap_seed: int
    bucket_seconds: int
    min_samples_per_bucket: int
    buckets: tuple[OnsetBucket, ...]


class Severity(_Model):
    """Severity v1 — a deterministic score in [0, 100], **not** a probability.

    ``s_lat = clamp(20·log2(p95_post / p95_base), 0, 50)`` if the p95 latency gate passed, else 0;
    ``s_err = clamp(8·(err_post_pp - err_base_pp), 0, 50)`` if the error-rate gate passed, else 0;
    ``score = min(100, s_lat + s_err)``; band: HEALTHY < 15 ≤ WARNING < 40 ≤ HIGH < 70 ≤ CRITICAL.
    """

    formula: Literal["severity-v1"] = "severity-v1"
    score: float
    band: SeverityBand
    latency_component: float
    error_component: float


class WindowSummary(_Model):
    """Descriptive statistics of one window (for display; decisions use the evidence models)."""

    requests: int
    errors: int
    error_rate: float | None
    throughput_rpm: float | None
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    latency_p99_ms: float | None
    self_time_p95_ms: float | None


class ServiceEvidence(_Model):
    """Everything the detector concluded about one service, and why."""

    service: str
    variant: str  # name of the WindowConfig used ("strict" or "relaxed")
    status: ServiceStatus
    sample_gate: SampleGate
    baseline: WindowSummary
    post: WindowSummary
    latency: tuple[LatencyEvidence, ...] = ()
    self_time: tuple[LatencyEvidence, ...] = ()
    error_rate: ErrorRateEvidence | None = None
    own_error_rate: ErrorRateEvidence | None = None
    throughput: ThroughputEvidence | None = None
    severity: Severity | None = None  # None when INSUFFICIENT_DATA (not assessed)
    onset: OnsetEvidence | None = None  # computed only when the p95 latency gates passed
    regressed_metrics: tuple[str, ...] = ()
    self_time_regressed: bool = False
    originates_errors: bool = False

    def latency_at(self, percentile: float) -> LatencyEvidence | None:
        return next((e for e in self.latency if e.percentile == percentile), None)

    def self_time_at(self, percentile: float) -> LatencyEvidence | None:
        return next((e for e in self.self_time if e.percentile == percentile), None)

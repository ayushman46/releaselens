"""``analysis.results`` events: a regression analysis finished (producer: worker job handler via
the transactional outbox; consumers: notifier, UI cache warmers).

Contract (docs/events/ANALYSIS_RESULTS.md). Key: ``"<tenant_id>:<deployment_id>"``. The envelope
carries a *summary*; the full evidence lives in ``regression_analyses.result`` and is fetched by
id. Wording rule: ``probable_origin``, never "root cause".
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from rl_core.events.errors import (
    EventTooLargeError,
    EventValidationError,
    MalformedEventError,
    UnknownEventTypeError,
    UnknownSchemaVersionError,
)
from rl_core.ids import uuid7

EVENT_TYPE_ANALYSIS_COMPLETED: Final = "analysis.completed"
ANALYSIS_SCHEMA_VERSION: Final = 1
MAX_ANALYSIS_ENVELOPE_BYTES: Final = 64 * 1024

Name = Annotated[str, StringConstraints(min_length=1, max_length=200)]

AnalysisKind = Literal["early_5m", "early_10m", "final"]
ServiceStatus = Literal["HEALTHY", "WARNING", "REGRESSED", "INSUFFICIENT_DATA"]
SeverityBand = Literal["HEALTHY", "WARNING", "HIGH", "CRITICAL"]
Confidence = Literal["HIGH", "MEDIUM", "LOW"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AnalysisSummary(_Strict):
    deployment_id: uuid.UUID
    analysis_id: uuid.UUID
    kind: AnalysisKind
    service: Name
    environment: Name
    version: Name
    previous_version: Name | None = None
    status: ServiceStatus
    severity_score: float | None = Field(default=None, ge=0, le=100)
    severity_band: SeverityBand | None = None
    probable_origin: Name | None = None
    confidence: Confidence | None = None
    affected_services: list[Name] = Field(default_factory=list, max_length=200)
    engine_version: Name
    completed_at: datetime


class AnalysisEnvelope(_Strict):
    schema_version: Literal[1]
    event_id: uuid.UUID
    tenant_id: uuid.UUID
    occurred_at: datetime
    type: Literal["analysis.completed"]
    payload: AnalysisSummary


def analysis_key(tenant_id: uuid.UUID, deployment_id: uuid.UUID) -> str:
    """Kafka key: keeps all analyses of one deployment in order on one partition."""
    return f"{tenant_id}:{deployment_id}"


def make_analysis_envelope(
    *, tenant_id: uuid.UUID, summary: AnalysisSummary, event_id: uuid.UUID | None = None
) -> AnalysisEnvelope:
    return AnalysisEnvelope(
        schema_version=ANALYSIS_SCHEMA_VERSION,
        event_id=event_id or uuid7(),
        tenant_id=tenant_id,
        occurred_at=datetime.now(UTC),
        type=EVENT_TYPE_ANALYSIS_COMPLETED,
        payload=summary,
    )


def encode_analysis(envelope: AnalysisEnvelope) -> bytes:
    data = envelope.model_dump_json().encode("utf-8")
    if len(data) > MAX_ANALYSIS_ENVELOPE_BYTES:
        raise EventTooLargeError(f"analysis event is {len(data)} bytes")
    return data


def parse_analysis(raw: bytes | bytearray | memoryview | None) -> AnalysisEnvelope:
    """Validate one ``analysis.results`` message; raises PermanentError subclasses."""
    if raw is None:
        raise MalformedEventError("event has no value (tombstone)")
    data = bytes(raw)
    if len(data) > MAX_ANALYSIS_ENVELOPE_BYTES:
        raise EventTooLargeError(f"event is {len(data)} bytes")
    try:
        obj: Any = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise MalformedEventError("event is not valid UTF-8 JSON") from exc
    if not isinstance(obj, dict):
        raise MalformedEventError("event must be a JSON object")
    if obj.get("type") != EVENT_TYPE_ANALYSIS_COMPLETED:
        raise UnknownEventTypeError(f"unknown event type {str(obj.get('type'))[:100]!r}")
    if obj.get("schema_version") != ANALYSIS_SCHEMA_VERSION or isinstance(
        obj.get("schema_version"), bool
    ):
        raise UnknownSchemaVersionError(f"unsupported schema_version {obj.get('schema_version')!r}")
    try:
        return AnalysisEnvelope.model_validate(obj)
    except ValidationError as exc:
        errors = [
            {"loc": ".".join(str(p) for p in e["loc"]), "type": e["type"], "msg": e["msg"]}
            for e in exc.errors(include_url=False, include_input=False)
        ]
        raise EventValidationError(f"{len(errors)} validation error(s)", errors) from None

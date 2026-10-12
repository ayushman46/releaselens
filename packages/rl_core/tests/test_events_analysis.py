import json
import uuid
from datetime import UTC, datetime

import pytest

from rl_core.events.analysis import (
    AnalysisSummary,
    analysis_key,
    encode_analysis,
    make_analysis_envelope,
    parse_analysis,
)
from rl_core.events.errors import (
    EventValidationError,
    MalformedEventError,
    UnknownEventTypeError,
    UnknownSchemaVersionError,
)


def _summary(**kw: object) -> AnalysisSummary:
    base: dict[str, object] = {
        "deployment_id": uuid.uuid4(),
        "analysis_id": uuid.uuid4(),
        "kind": "final",
        "service": "payment",
        "environment": "demo",
        "version": "v2",
        "previous_version": "v1",
        "status": "REGRESSED",
        "severity_score": 58.6,
        "severity_band": "HIGH",
        "probable_origin": "payment",
        "confidence": "HIGH",
        "affected_services": ["orders", "gateway"],
        "engine_version": "regression-engine/1.0.0",
        "completed_at": datetime(2026, 10, 13, tzinfo=UTC),
    }
    base.update(kw)
    return AnalysisSummary.model_validate(base)


def test_round_trip() -> None:
    tenant = uuid.uuid4()
    env = make_analysis_envelope(tenant_id=tenant, summary=_summary())
    parsed = parse_analysis(encode_analysis(env))
    assert parsed == env
    assert (
        analysis_key(tenant, env.payload.deployment_id) == f"{tenant}:{env.payload.deployment_id}"
    )


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        (None, MalformedEventError),
        (b"not json", MalformedEventError),
        (b"[]", MalformedEventError),
        (b'{"type": "deployment.completed", "schema_version": 1}', UnknownEventTypeError),
        (b'{"type": "analysis.completed", "schema_version": 2}', UnknownSchemaVersionError),
        (b'{"type": "analysis.completed", "schema_version": true}', UnknownSchemaVersionError),
        (b'{"type": "analysis.completed", "schema_version": 1}', EventValidationError),
    ],
)
def test_rejects_invalid(raw: bytes | None, error: type[Exception]) -> None:
    with pytest.raises(error):
        parse_analysis(raw)


def test_rejects_unknown_fields_and_bad_values() -> None:
    env = make_analysis_envelope(tenant_id=uuid.uuid4(), summary=_summary())
    obj = json.loads(encode_analysis(env))
    obj["payload"]["root_cause"] = "payment"
    with pytest.raises(EventValidationError):
        parse_analysis(json.dumps(obj).encode())
    with pytest.raises(ValueError):
        _summary(severity_score=101)

"""Deployment envelope parsing: versions, upcasting, strictness and limits."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rl_core.events import (
    DEPLOYMENT_EVENTS_DLQ,
    MAX_ENVELOPE_BYTES,
    DeploymentPayloadV1,
    DeploymentPayloadV2,
    EventTooLargeError,
    EventValidationError,
    MalformedEventError,
    PermanentError,
    UnknownEventTypeError,
    UnknownSchemaVersionError,
    deployment_key,
    encode_envelope,
    make_envelope,
    parse_envelope,
)
from rl_core.ids import uuid7

TENANT = uuid.UUID("0192a7c0-0000-7000-8000-000000000001")


def _payload_v1(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "service": "payment",
        "environment": "production",
        "version": "v2.4.1",
        "previous_version": "v2.4.0",
        "commit_sha": "0123456789abcdef0123456789abcdef01234567",
        "deployed_at": "2026-10-13T12:00:00Z",
        "deployer": "ci-bot",
        "ci_run_url": "https://github.com/acme/payment/actions/runs/42",
    }
    payload.update(overrides)
    return payload


def _envelope(version: int = 2, payload: dict[str, Any] | None = None, **top: Any) -> bytes:
    body: dict[str, Any] = {
        "schema_version": version,
        "event_id": str(uuid7()),
        "tenant_id": str(TENANT),
        "occurred_at": "2026-10-13T12:00:01+00:00",
        "type": "deployment.completed",
        "payload": payload if payload is not None else _payload_v1(),
    }
    body.update(top)
    return json.dumps(body).encode()


# ----------------------------------------------------------------------------- happy paths
def test_v1_is_upcast_with_v2_fields_empty() -> None:
    event = parse_envelope(_envelope(1))
    assert event.schema_version == 1
    assert event.tenant_id == TENANT
    assert event.service == "payment"
    assert event.deployed_at == datetime(2026, 10, 13, 12, tzinfo=UTC)
    assert event.repository is None
    assert event.pull_request is None
    assert event.risk_assessment_id is None
    assert dict(event.labels) == {}


def test_v2_carries_its_extra_fields() -> None:
    risk_id = uuid.uuid4()
    payload = _payload_v1(
        repository="acme/payment",
        pull_request=1234,
        risk_assessment_id=str(risk_id),
        labels={"team": "payments", "region": "eu-west-1"},
    )
    event = parse_envelope(_envelope(2, payload))
    assert event.schema_version == 2
    assert event.repository == "acme/payment"
    assert event.pull_request == 1234
    assert event.risk_assessment_id == risk_id
    assert dict(event.labels) == {"team": "payments", "region": "eu-west-1"}


def test_optional_fields_may_be_omitted() -> None:
    minimal = {k: v for k, v in _payload_v1().items() if k not in {"previous_version", "deployer"}}
    minimal.pop("ci_run_url")
    event = parse_envelope(_envelope(2, minimal))
    assert event.previous_version is None
    assert event.deployer is None
    assert event.ci_run_url is None


def test_timestamps_are_normalised_to_utc() -> None:
    event = parse_envelope(_envelope(2, _payload_v1(deployed_at="2026-10-13T14:00:00+02:00")))
    assert event.deployed_at == datetime(2026, 10, 13, 12, tzinfo=UTC)
    assert event.deployed_at.utcoffset() == timedelta(0)


def test_commit_sha_is_lowercased() -> None:
    event = parse_envelope(_envelope(2, _payload_v1(commit_sha="ABCDEF1")))
    assert event.commit_sha == "abcdef1"


def test_any_uuid_version_is_accepted_as_event_id() -> None:
    event_id = uuid.uuid4()
    assert parse_envelope(_envelope(2, event_id=str(event_id))).event_id == event_id


def test_labels_are_read_only() -> None:
    event = parse_envelope(_envelope(2, _payload_v1(labels={"a": "b"})))
    with pytest.raises(TypeError):
        event.labels["a"] = "c"  # type: ignore[index]


# ----------------------------------------------------------------------------- rejections
@pytest.mark.parametrize(
    "raw",
    [b"", b"not json", b"\xff\xfe", b"[1,2]", b'"str"', b"{", b'{"a": NaN}'],
    ids=["empty", "text", "not-utf8", "array", "string", "truncated", "nan"],
)
def test_malformed_bytes_are_permanent(raw: bytes) -> None:
    with pytest.raises(MalformedEventError) as info:
        parse_envelope(raw)
    assert info.value.error_type == "malformed_json"
    assert isinstance(info.value, PermanentError)


def test_tombstone_is_malformed() -> None:
    with pytest.raises(MalformedEventError):
        parse_envelope(None)


def test_duplicate_keys_are_rejected() -> None:
    raw = _envelope(2).replace(b'"type": "deployment.completed"', b'"type": "x", "type": "y"')
    with pytest.raises(MalformedEventError, match="duplicate key"):
        parse_envelope(raw)


@pytest.mark.parametrize("version", [0, 3, 99, -1, "2", 2.0, True, None, [2]])
def test_unknown_schema_versions(version: object) -> None:
    with pytest.raises(UnknownSchemaVersionError) as info:
        parse_envelope(_envelope(schema_version=version))
    assert info.value.error_type == "unknown_schema_version"


def test_missing_schema_version() -> None:
    body = json.loads(_envelope())
    del body["schema_version"]
    with pytest.raises(UnknownSchemaVersionError, match="missing"):
        parse_envelope(json.dumps(body).encode())


def test_unknown_event_type() -> None:
    with pytest.raises(UnknownEventTypeError) as info:
        parse_envelope(_envelope(type="deployment.started"))
    assert info.value.error_type == "unknown_event_type"


@pytest.mark.parametrize(
    "field",
    ["service", "environment", "version", "commit_sha", "deployed_at"],
)
def test_missing_required_payload_field(field: str) -> None:
    payload = _payload_v1()
    del payload[field]
    with pytest.raises(EventValidationError) as info:
        parse_envelope(_envelope(2, payload))
    assert info.value.error_type == "validation_error"
    assert any(e["loc"] == f"payload.{field}" for e in info.value.errors)


@pytest.mark.parametrize(
    ("overrides", "loc"),
    [
        ({"service": ""}, "payload.service"),
        ({"service": "x" * 201}, "payload.service"),
        ({"service": "bad name"}, "payload.service"),
        ({"environment": "prod\n"}, "payload.environment"),
        ({"version": "1.0 beta"}, "payload.version"),
        ({"commit_sha": "xyz1234"}, "payload.commit_sha"),
        ({"commit_sha": "abc12"}, "payload.commit_sha"),
        ({"deployed_at": "2026-10-13T12:00:00"}, "payload.deployed_at"),  # naive
        ({"deployed_at": 1_760_000_000}, "payload.deployed_at"),  # epoch number
        ({"deployed_at": "1970-01-01T00:00:00Z"}, "payload.deployed_at"),
        ({"deployer": "evil\nINFO forged"}, "payload.deployer"),
        ({"ci_run_url": "javascript:alert(1)"}, "payload.ci_run_url"),
        ({"ci_run_url": "https://" + "a" * 2050}, "payload.ci_run_url"),
        ({"pull_request": "12"}, "payload.pull_request"),
        ({"pull_request": 0}, "payload.pull_request"),
        ({"pull_request": 2**31}, "payload.pull_request"),
        ({"repository": "no-slash"}, "payload.repository"),
        ({"labels": {"k": 1}}, "payload.labels.k"),
        ({"labels": {"bad key": "v"}}, "payload.labels.bad key.[key]"),
        ({"labels": {"k": "v" * 257}}, "payload.labels.k"),
        ({"labels": {f"k{i}": "v" for i in range(33)}}, "payload.labels"),
        ({"unexpected": "field"}, "payload.unexpected"),
    ],
)
def test_v2_field_limits(overrides: dict[str, Any], loc: str) -> None:
    with pytest.raises(EventValidationError) as info:
        parse_envelope(_envelope(2, _payload_v1(**overrides)))
    assert loc in [e["loc"] for e in info.value.errors]


def test_v1_rejects_v2_only_fields() -> None:
    with pytest.raises(EventValidationError) as info:
        parse_envelope(_envelope(1, _payload_v1(repository="acme/payment")))
    assert "payload.repository" in [e["loc"] for e in info.value.errors]


@pytest.mark.parametrize(
    ("top", "loc"),
    [
        ({"event_id": "not-a-uuid"}, "event_id"),
        ({"event_id": str(uuid.UUID(int=0))}, "event_id"),
        ({"tenant_id": 42}, "tenant_id"),
        ({"occurred_at": "yesterday"}, "occurred_at"),
        ({"extra": True}, "extra"),
    ],
)
def test_envelope_field_errors(top: dict[str, Any], loc: str) -> None:
    with pytest.raises(EventValidationError) as info:
        parse_envelope(_envelope(2, **top))
    assert loc in [e["loc"] for e in info.value.errors]


def test_validation_errors_never_echo_input_values() -> None:
    secret = "s3cr3t-token-value"
    with pytest.raises(EventValidationError) as info:
        parse_envelope(_envelope(2, _payload_v1(deployer=secret + "\n")))
    assert secret not in str(info.value)
    assert secret not in json.dumps(info.value.errors)
    assert info.value.__cause__ is None


def test_oversized_event_is_rejected_before_parsing() -> None:
    raw = b"{" + b" " * MAX_ENVELOPE_BYTES + b"}"
    with pytest.raises(EventTooLargeError):
        parse_envelope(raw)


# ----------------------------------------------------------------------------- encoding
def test_make_envelope_picks_version_from_payload_class() -> None:
    v1 = DeploymentPayloadV1.model_validate(_payload_v1())
    v2 = DeploymentPayloadV2.model_validate(_payload_v1(labels={"a": "b"}))
    assert make_envelope(tenant_id=TENANT, payload=v1).schema_version == 1
    assert make_envelope(tenant_id=TENANT, payload=v2).schema_version == 2


def test_encoded_envelope_round_trips() -> None:
    payload = DeploymentPayloadV2.model_validate(
        _payload_v1(repository="acme/pay", pull_request=7, labels={"tier": "1"})
    )
    occurred = datetime(2026, 10, 13, 9, 30, tzinfo=timezone(timedelta(hours=-3)))
    env = make_envelope(tenant_id=TENANT, payload=payload, occurred_at=occurred)
    event = parse_envelope(encode_envelope(env))
    assert event.event_id == env.event_id
    assert event.event_id.version == 7
    assert event.occurred_at == occurred
    assert event.repository == "acme/pay"
    assert dict(event.labels) == {"tier": "1"}


def test_topic_helpers() -> None:
    assert DEPLOYMENT_EVENTS_DLQ == "deployment.events.dlq"
    assert deployment_key(TENANT, "payment") == f"{TENANT}:payment"


_label_keys = st.from_regex(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,20}", fullmatch=True)
_label_values = st.text(
    alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x2FF, exclude_characters="\x7f"),
    max_size=40,
)


@settings(max_examples=150, deadline=None)
@given(
    service=st.from_regex(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,40}", fullmatch=True),
    environment=st.from_regex(r"[A-Za-z0-9][A-Za-z0-9._-]{0,20}", fullmatch=True),
    version=st.from_regex(r"[!-~]{1,30}", fullmatch=True),
    sha=st.from_regex(r"[0-9a-f]{7,64}", fullmatch=True),
    deployed_at=st.datetimes(
        min_value=datetime(2000, 1, 2), max_value=datetime(2099, 12, 30), timezones=st.just(UTC)
    ),
    pull_request=st.none() | st.integers(min_value=1, max_value=2**31 - 1),
    labels=st.dictionaries(_label_keys, _label_values, max_size=32),
)
def test_round_trip_property(
    service: str,
    environment: str,
    version: str,
    sha: str,
    deployed_at: datetime,
    pull_request: int | None,
    labels: dict[str, str],
) -> None:
    payload = DeploymentPayloadV2(
        service=service,
        environment=environment,
        version=version,
        commit_sha=sha,
        deployed_at=deployed_at,
        pull_request=pull_request,
        labels=labels,
    )
    event = parse_envelope(encode_envelope(make_envelope(tenant_id=TENANT, payload=payload)))
    assert (event.service, event.environment, event.version) == (service, environment, version)
    assert event.commit_sha == sha
    assert event.deployed_at == deployed_at
    assert event.pull_request == pull_request
    assert dict(event.labels) == labels

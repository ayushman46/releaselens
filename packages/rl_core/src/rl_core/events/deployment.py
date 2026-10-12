"""``deployment.completed`` events: versioned envelopes and the canonical internal event.

Wire format (one JSON object per Kafka message on ``deployment.events``)::

    {"schema_version": 2, "event_id": "<uuid>", "tenant_id": "<uuid>",
     "occurred_at": "2026-10-13T12:00:00Z", "type": "deployment.completed",
     "payload": {...}}

* **v1** payload: ``service``, ``environment``, ``version``, ``previous_version?``,
  ``commit_sha``, ``deployed_at``, ``deployer?``, ``ci_run_url?``.
* **v2** = v1 + ``repository?`` (``owner/name``), ``pull_request?``,
  ``risk_assessment_id?``, ``labels`` (bounded ``str -> str`` map).

Producers may send any supported version. :func:`parse_envelope` validates the bytes
against that version's schema and *upcasts* the result to one canonical
:class:`DeploymentEvent`, so nothing downstream ever branches on the schema version.
Adding v3 means a new envelope model, one more entry in ``_ENVELOPES`` and an upcast
branch. Old producers keep working.

Validation is deliberately strict. Wrong JSON types are rejected rather than coerced
(``"3"`` is not a pull request number, ``true`` is not schema version 1), unknown fields
are rejected, and every string and collection has an upper bound. The size of one
envelope is bounded by :data:`MAX_ENVELOPE_BYTES`, so a single bad producer cannot push
unbounded data into Postgres or the DLQ.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Annotated, Any, Final, Literal
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
)

from rl_core.events.errors import (
    EventTooLargeError,
    EventValidationError,
    MalformedEventError,
    UnknownEventTypeError,
    UnknownSchemaVersionError,
)
from rl_core.ids import uuid7

EVENT_TYPE_DEPLOYMENT_COMPLETED: Final = "deployment.completed"
SUPPORTED_SCHEMA_VERSIONS: Final = (1, 2)
LATEST_SCHEMA_VERSION: Final = 2

# Hard ceiling for one serialized envelope. The field limits below bound a valid
# envelope at roughly 12 KB of ASCII; the headroom covers \\uXXXX-escaped non-ASCII text.
MAX_ENVELOPE_BYTES: Final = 64 * 1024

# Field limits. ``rl_db.models.deployments`` sizes its columns from these constants.
MAX_SERVICE_LEN: Final = 200
MAX_ENVIRONMENT_LEN: Final = 64
MAX_VERSION_LEN: Final = 200
MAX_COMMIT_SHA_LEN: Final = 64
MAX_DEPLOYER_LEN: Final = 200
MAX_URL_LEN: Final = 2048
MAX_REPOSITORY_LEN: Final = 201  # 100 + "/" + 100
MAX_LABELS: Final = 32
MAX_LABEL_KEY_LEN: Final = 63
MAX_LABEL_VALUE_LEN: Final = 256
MAX_PULL_REQUEST: Final = 2**31 - 1  # Postgres INTEGER

# Timestamps outside this range are almost certainly unit or clock bugs (epoch seconds
# sent as milliseconds, a zeroed clock). Rejecting them keeps the analysis jobs, which run
# at deployed_at + N minutes, from being scheduled decades away.
_MIN_YEAR: Final = 2000
_MAX_YEAR: Final = 2100

V2_ONLY_FIELDS: Final = frozenset({"repository", "pull_request", "risk_assessment_id", "labels"})


# ----------------------------------------------------------------------------- field types
def _require_iso_string(value: Any) -> Any:
    # Pydantic's lax datetime parsing also accepts numbers as Unix time. A number here is
    # ambiguous (seconds or milliseconds?), so only ISO-8601 strings and datetimes pass.
    if isinstance(value, str | datetime):
        return value
    raise ValueError("timestamp must be an ISO-8601 string with a UTC offset")


def _to_utc(value: datetime) -> datetime:
    if not _MIN_YEAR <= value.year <= _MAX_YEAR:
        raise ValueError(f"timestamp year must be within {_MIN_YEAR}..{_MAX_YEAR}")
    return value.astimezone(UTC)


def _not_nil(value: uuid.UUID) -> uuid.UUID:
    if value.int == 0:
        raise ValueError("the nil UUID is not a valid identifier")
    return value


def _http_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("must be an absolute http(s) URL")
    return value


def _reject_bool(value: Any) -> Any:
    # ``True == 1`` in Python, and pydantic's Literal check accepts it even in strict mode.
    if isinstance(value, bool):
        raise ValueError("schema_version must be an integer")
    return value


UtcTimestamp = Annotated[
    AwareDatetime,
    Field(strict=False),
    BeforeValidator(_require_iso_string),
    AfterValidator(_to_utc),
]
Identifier = Annotated[uuid.UUID, Field(strict=False), AfterValidator(_not_nil)]

# A service name as it appears in OTel ``service.name`` and in Kafka keys.
ServiceName = Annotated[
    str,
    StringConstraints(
        min_length=1, max_length=MAX_SERVICE_LEN, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@-]*$"
    ),
]
EnvironmentName = Annotated[
    str,
    StringConstraints(
        min_length=1, max_length=MAX_ENVIRONMENT_LEN, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
    ),
]
# Printable ASCII without spaces covers semver, calver, image tags and git describe output.
VersionString = Annotated[
    str, StringConstraints(min_length=1, max_length=MAX_VERSION_LEN, pattern=r"^[!-~]+$")
]
CommitSha = Annotated[
    str,
    StringConstraints(to_lower=True, pattern=r"^[0-9a-fA-F]{7,64}$"),
]
# Free text (a name or an e-mail address), but never control characters, which could
# forge log lines.
FreeText = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_DEPLOYER_LEN, pattern=r"^[^\x00-\x1f\x7f]+$"),
]
HttpUrl = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_URL_LEN, pattern=r"^[!-~]+$"),
    AfterValidator(_http_url),
]
Repository = Annotated[
    str,
    StringConstraints(
        max_length=MAX_REPOSITORY_LEN, pattern=r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$"
    ),
]
PullRequestNumber = Annotated[int, Field(ge=1, le=MAX_PULL_REQUEST)]
LabelKey = Annotated[
    str,
    StringConstraints(
        min_length=1, max_length=MAX_LABEL_KEY_LEN, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$"
    ),
]
LabelValue = Annotated[
    str, StringConstraints(max_length=MAX_LABEL_VALUE_LEN, pattern=r"^[^\x00-\x1f\x7f]*$")
]
Labels = Annotated[dict[LabelKey, LabelValue], Field(max_length=MAX_LABELS)]


# ----------------------------------------------------------------------------- wire models
class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class DeploymentPayloadV1(_StrictModel):
    """Payload of a schema-version-1 ``deployment.completed`` event."""

    service: ServiceName
    environment: EnvironmentName
    version: VersionString
    previous_version: VersionString | None = None
    commit_sha: CommitSha
    deployed_at: UtcTimestamp
    deployer: FreeText | None = None
    ci_run_url: HttpUrl | None = None


class DeploymentPayloadV2(DeploymentPayloadV1):
    """v1 plus links to the code review and the pre-deploy risk assessment."""

    repository: Repository | None = None
    pull_request: PullRequestNumber | None = None
    risk_assessment_id: Identifier | None = None
    labels: Labels = Field(default_factory=dict)


class _EnvelopeBase(_StrictModel):
    event_id: Identifier
    tenant_id: Identifier
    occurred_at: UtcTimestamp
    type: Literal["deployment.completed"]


class DeploymentEnvelopeV1(_EnvelopeBase):
    schema_version: Annotated[Literal[1], BeforeValidator(_reject_bool)]
    payload: DeploymentPayloadV1


class DeploymentEnvelopeV2(_EnvelopeBase):
    schema_version: Annotated[Literal[2], BeforeValidator(_reject_bool)]
    payload: DeploymentPayloadV2


DeploymentEnvelope = DeploymentEnvelopeV1 | DeploymentEnvelopeV2

_ENVELOPES: Final[dict[int, type[DeploymentEnvelopeV1] | type[DeploymentEnvelopeV2]]] = {
    1: DeploymentEnvelopeV1,
    2: DeploymentEnvelopeV2,
}


# ----------------------------------------------------------------------------- canonical
@dataclass(frozen=True, slots=True, kw_only=True)
class DeploymentEvent:
    """The one internal shape of a deployment event, whatever version it arrived as.

    ``schema_version`` records the wire version for audit only. Consumers must not branch
    on it: fields a version lacks are ``None`` (``labels``: empty).
    """

    event_id: uuid.UUID
    tenant_id: uuid.UUID
    occurred_at: datetime
    schema_version: int
    service: str
    environment: str
    version: str
    previous_version: str | None
    commit_sha: str
    deployed_at: datetime
    deployer: str | None
    ci_run_url: str | None
    repository: str | None
    pull_request: int | None
    risk_assessment_id: uuid.UUID | None
    labels: Mapping[str, str]


def to_event(envelope: DeploymentEnvelope) -> DeploymentEvent:
    """Upcast a validated envelope (any version) to the canonical event."""
    p = envelope.payload
    if isinstance(p, DeploymentPayloadV2):
        repository, pull_request, risk_id = p.repository, p.pull_request, p.risk_assessment_id
        labels: Mapping[str, str] = MappingProxyType(dict(p.labels))
    else:
        repository, pull_request, risk_id = None, None, None
        labels = MappingProxyType({})
    return DeploymentEvent(
        event_id=envelope.event_id,
        tenant_id=envelope.tenant_id,
        occurred_at=envelope.occurred_at,
        schema_version=envelope.schema_version,
        service=p.service,
        environment=p.environment,
        version=p.version,
        previous_version=p.previous_version,
        commit_sha=p.commit_sha,
        deployed_at=p.deployed_at,
        deployer=p.deployer,
        ci_run_url=p.ci_run_url,
        repository=repository,
        pull_request=pull_request,
        risk_assessment_id=risk_id,
        labels=labels,
    )


# ----------------------------------------------------------------------------- decoding
def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-standard JSON constant {name}")


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    # Duplicate keys are legal JSON but parsers disagree on which value wins. A message
    # whose meaning depends on the parser is rejected instead of silently interpreted.
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate key {key!r}")
        obj[key] = value
    return obj


def _decode_json_object(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
        obj = json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_unique_keys)
    except UnicodeDecodeError as exc:
        raise MalformedEventError("event is not valid UTF-8") from exc
    except (ValueError, RecursionError) as exc:  # JSONDecodeError is a ValueError
        raise MalformedEventError(f"event is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise MalformedEventError("event must be a JSON object")
    return obj


def _summarize(exc: ValidationError) -> list[dict[str, str]]:
    return [
        {
            "loc": ".".join(str(part) for part in err["loc"]),
            "type": err["type"],
            "msg": err["msg"],
        }
        for err in exc.errors(include_url=False, include_input=False)
    ]


def parse_envelope(raw: bytes | bytearray | memoryview | None) -> DeploymentEvent:
    """Validate one wire message and return the canonical event.

    Raises a :class:`~rl_core.events.errors.PermanentError` subclass for every input that
    can never become valid: too large, not JSON, unknown ``type``, unknown
    ``schema_version`` or a schema violation. The checks run cheapest first.
    """
    if raw is None:
        raise MalformedEventError("event has no value (tombstone)")
    data = bytes(raw)
    if len(data) > MAX_ENVELOPE_BYTES:
        raise EventTooLargeError(f"event is {len(data)} bytes; limit is {MAX_ENVELOPE_BYTES}")
    obj = _decode_json_object(data)

    event_type = obj.get("type")
    if isinstance(event_type, str) and event_type != EVENT_TYPE_DEPLOYMENT_COMPLETED:
        raise UnknownEventTypeError(f"unknown event type {event_type[:100]!r}")

    version = obj.get("schema_version")
    if "schema_version" not in obj:
        raise UnknownSchemaVersionError("schema_version is missing")
    if isinstance(version, bool) or not isinstance(version, int) or version not in _ENVELOPES:
        raise UnknownSchemaVersionError(
            f"unsupported schema_version {str(version)[:20]!r}; "
            f"supported: {list(SUPPORTED_SCHEMA_VERSIONS)}"
        )

    try:
        envelope = _ENVELOPES[version].model_validate(obj)
    except ValidationError as exc:
        # ``from None``: pydantic's own message embeds the rejected input values, and the
        # chained traceback would carry them into logs and the DLQ ``stack`` header.
        errors = _summarize(exc)
        first = errors[0] if errors else {"loc": "?", "msg": "invalid"}
        raise EventValidationError(
            f"{len(errors)} validation error(s); first: {first['loc']}: {first['msg']}",
            errors,
        ) from None
    return to_event(envelope)


# ----------------------------------------------------------------------------- encoding
def make_envelope(
    *,
    tenant_id: uuid.UUID,
    payload: DeploymentPayloadV1,
    event_id: uuid.UUID | None = None,
    occurred_at: datetime | None = None,
) -> DeploymentEnvelope:
    """Wrap a validated payload. The payload's class picks the schema version.

    ``event_id`` defaults to a fresh UUIDv7 (time-ordered, so it doubles as an arrival
    order hint). ``occurred_at`` is when the producer created the envelope, which is not
    necessarily ``deployed_at``.
    """
    common: dict[str, Any] = {
        "event_id": event_id or uuid7(),
        "tenant_id": tenant_id,
        "occurred_at": occurred_at or datetime.now(UTC),
        "type": EVENT_TYPE_DEPLOYMENT_COMPLETED,
    }
    if isinstance(payload, DeploymentPayloadV2):
        return DeploymentEnvelopeV2(schema_version=2, payload=payload, **common)
    return DeploymentEnvelopeV1(schema_version=1, payload=payload, **common)


def encode_envelope(envelope: DeploymentEnvelope) -> bytes:
    """Serialize for Kafka. The result always round-trips through :func:`parse_envelope`."""
    data = envelope.model_dump_json().encode("utf-8")
    if len(data) > MAX_ENVELOPE_BYTES:
        raise EventTooLargeError(f"event is {len(data)} bytes; limit is {MAX_ENVELOPE_BYTES}")
    return data

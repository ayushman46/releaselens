"""OTLP/HTTP trace ingest: ``POST /v1/traces`` (spec §11.2, ADR-0008).

Flow: authenticate (scope ``telemetry:write``) → check content type and encoding → read the body
under a size cap → gunzip under a decompressed-size cap → decode → validate and map each span
(:mod:`rl_api.otlp`) → produce one ``telemetry.spans`` message per service (more when a service's
spans exceed one message's size) keyed ``tenant_id:service.name`` → answer **after** the broker
acknowledged every message.

Responses follow OTLP/HTTP: ``200`` with an ``ExportTraceServiceResponse`` in the request's
encoding, carrying ``partial_success`` when some spans were rejected. Failures use the API's JSON
error envelope: ``400`` undecodable body, ``401``/``403`` key or scope, ``413`` body too large
(compressed or decompressed), ``415`` unsupported content type or encoding, ``503`` +
``Retry-After`` when Kafka did not acknowledge (the Collector retries the whole request; the
consumer deduplicates spans already stored).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response

from rl_api.auth import require_scope
from rl_api.deps import get_producer
from rl_api.errors import ApiError
from rl_api.kafka import EventProducer, ProduceError
from rl_api.otlp import (
    ConvertedRequest,
    Encoding,
    OtlpDecodeError,
    PayloadTooLargeError,
    TimeBounds,
    convert_request,
    decode_request,
    encode_response,
    gunzip_bounded,
)
from rl_core.events import TELEMETRY_SPANS
from rl_core.events.spans import (
    EVENT_TYPE_SPAN_BATCH,
    SPAN_BATCH_SCHEMA_VERSION,
    encode_span_batches,
)
from rl_core.events.topics import span_key
from rl_core.tenancy import TenantContext

log = logging.getLogger("rl_api.otlp")

router = APIRouter(tags=["telemetry"])

_CONTENT_TYPES: dict[str, Encoding] = {
    "application/x-protobuf": "protobuf",
    "application/protobuf": "protobuf",
    "application/json": "json",
}
_HEADERS = [
    ("content-type", b"application/json"),
    ("rl-event-type", EVENT_TYPE_SPAN_BATCH.encode()),
    ("rl-schema-version", str(SPAN_BATCH_SCHEMA_VERSION).encode()),
]

_OPENAPI_BODY: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "description": "OTLP ExportTraceServiceRequest (protobuf or OTLP/JSON), optionally gzip",
        "content": {
            "application/x-protobuf": {"schema": {"type": "string", "format": "binary"}},
            "application/json": {"schema": {"type": "object"}},
        },
    }
}


def _encoding(request: Request) -> Encoding:
    media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    encoding = _CONTENT_TYPES.get(media)
    if encoding is None:
        raise ApiError(
            415,
            "unsupported_media_type",
            "Content-Type must be application/x-protobuf or application/json",
        )
    content_encoding = request.headers.get("content-encoding", "identity").strip().lower()
    if content_encoding not in ("identity", "gzip", ""):
        raise ApiError(415, "unsupported_media_type", "Content-Encoding must be gzip or identity")
    return encoding


def _too_large(message: str) -> ApiError:
    return ApiError(413, "payload_too_large", message)


async def _read_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise _too_large(f"request body exceeds {limit} bytes")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise _too_large(f"request body exceeds {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


async def _produce(producer: EventProducer, ctx: TenantContext, converted: ConvertedRequest) -> int:
    received_at = datetime.now(UTC)
    sends = [
        producer.produce(
            TELEMETRY_SPANS, key=span_key(ctx.tenant_id, service), value=value, headers=_HEADERS
        )
        for service, spans in sorted(converted.by_service.items())
        for value in encode_span_batches(
            tenant_id=ctx.tenant_id, service=service, spans=spans, received_at=received_at
        )
    ]
    results = await asyncio.gather(*sends, return_exceptions=True)
    failures = [r for r in results if isinstance(r, BaseException)]
    if failures:
        unexpected = [f for f in failures if not isinstance(f, ProduceError)]
        if unexpected:
            raise unexpected[0]
        log.warning(
            "span batches not acknowledged by kafka",
            extra={"messages": len(sends), "failed": len(failures), "error": str(failures[0])},
        )
        raise ApiError(
            503,
            "event_bus_unavailable",
            "telemetry could not be recorded; retry the export",
            headers={"Retry-After": "5"},
        )
    return len(sends)


@router.post(
    "/v1/traces",
    summary="OTLP/HTTP trace export",
    response_class=Response,
    openapi_extra=_OPENAPI_BODY,
    responses={
        200: {
            "description": "ExportTraceServiceResponse (partial_success when spans were rejected)",
            "content": {"application/x-protobuf": {}, "application/json": {}},
        },
        400: {"description": "Body is not a decodable OTLP trace request"},
        401: {"description": "Missing or invalid API key"},
        403: {"description": "The API key lacks the telemetry:write scope"},
        413: {"description": "Body (compressed or decompressed) exceeds the limit"},
        415: {"description": "Unsupported Content-Type or Content-Encoding"},
        503: {"description": "Kafka did not acknowledge the spans; retry (see Retry-After)"},
    },
)
async def export_traces(
    request: Request,
    ctx: Annotated[TenantContext, Depends(require_scope("telemetry:write"))],
    producer: Annotated[EventProducer, Depends(get_producer)],
) -> Response:
    settings = request.app.state.settings
    encoding = _encoding(request)
    body = await _read_body(request, settings.otlp_max_body_bytes)
    try:
        if request.headers.get("content-encoding", "").strip().lower() == "gzip":
            body = gunzip_bounded(body, settings.otlp_max_decompressed_bytes)
        decoded = decode_request(body, encoding)
    except PayloadTooLargeError as exc:
        raise _too_large(str(exc)) from exc
    except OtlpDecodeError as exc:
        raise ApiError(400, "invalid_otlp", str(exc)) from exc

    started = time.perf_counter()
    bounds = TimeBounds(
        now_ns=time.time_ns(),
        max_future_ns=int(settings.otlp_max_future_skew_s * 1e9),
        max_age_ns=int(settings.otlp_max_span_age_s * 1e9),
    )
    converted = convert_request(decoded, bounds)
    messages = await _produce(producer, ctx, converted) if converted.accepted else 0
    payload, media_type = encode_response(converted.rejected, converted.error_message(), encoding)
    log_fields = {
        "accepted": converted.accepted,
        "rejected": converted.rejected,
        "services": len(converted.by_service),
        "messages": messages,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    }
    if converted.rejected:
        reasons = dict(converted.rejections)
        log.info("spans partially rejected", extra={**log_fields, "reasons": reasons})
    else:
        log.debug("spans accepted", extra=log_fields)
    return Response(content=payload, media_type=media_type)

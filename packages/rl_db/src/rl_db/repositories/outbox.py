"""Transactional outbox writes (tenant-scoped). The worker's relay publishes the rows."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from rl_core.ids import uuid7
from rl_core.tenancy import TenantContext
from rl_db.models.outbox import MAX_KEY_LEN, MAX_TOPIC_LEN, RESERVED_HEADERS, OutboxEvent
from rl_db.repositories._tenancy import require_tenant, tenant_scoped

# Payload, key and headers together stay well under Kafka's default 1 MB message limit. A
# row too large to publish would fail on every relay round, so it is refused at write time.
MAX_PAYLOAD_BYTES = 512 * 1024
MAX_HEADERS_BYTES = 16 * 1024
# Kafka's legal topic characters. A name the broker would reject can never be published.
_TOPIC_RE = re.compile(r"^[A-Za-z0-9._-]+$")


@tenant_scoped
async def add_outbox_event(
    session: AsyncSession,
    ctx: TenantContext,
    topic: str,
    key: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str] | None = None,
) -> uuid.UUID:
    """Queue a Kafka message, atomically with the caller's other writes.

    Call it inside the transaction that makes the domain change. If that transaction rolls
    back, the message is never sent; if it commits, the relay sends it at least once.
    ``payload`` must carry ``tenant_id`` (ADR-0007: payloads always repeat it), and it is
    checked here so a forgotten field fails at write time, not in a downstream consumer.
    """
    if not topic or len(topic) > MAX_TOPIC_LEN:
        raise ValueError(f"topic must be 1..{MAX_TOPIC_LEN} characters")
    if not _TOPIC_RE.fullmatch(topic) or topic in {".", ".."}:
        raise ValueError("topic may contain only ASCII letters, digits, '.', '_' and '-'")
    if not key or len(key) > MAX_KEY_LEN:
        raise ValueError(f"key must be 1..{MAX_KEY_LEN} characters")
    if "tenant_id" not in payload:
        raise ValueError("payload must include tenant_id")
    require_tenant(ctx, uuid.UUID(str(payload["tenant_id"])))
    encoded = json.dumps(payload, separators=(",", ":"), default=str)
    if len(encoded.encode()) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"payload exceeds {MAX_PAYLOAD_BYTES} bytes")
    clean_headers = {str(k): str(v) for k, v in (headers or {}).items()}
    reserved = sorted(RESERVED_HEADERS & {k.lower() for k in clean_headers})
    if reserved:
        raise ValueError(f"headers set by the relay cannot be supplied: {reserved}")
    if any(not k for k in clean_headers):
        raise ValueError("header names must not be empty")
    header_bytes = sum(len(k.encode()) + len(v.encode()) for k, v in clean_headers.items())
    if header_bytes > MAX_HEADERS_BYTES:
        raise ValueError(f"headers exceed {MAX_HEADERS_BYTES} bytes")

    row_id = uuid7()
    await session.execute(
        insert(OutboxEvent).values(
            id=row_id,
            tenant_id=ctx.tenant_id,
            topic=topic,
            key=key,
            # Round-trip through JSON so UUIDs and datetimes are stored as strings,
            # exactly as consumers will read them.
            payload=json.loads(encoded),
            headers=clean_headers,
        )
    )
    return row_id

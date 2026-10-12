"""Transactional outbox (ADR-0011).

A domain write and the Kafka message announcing it must both happen or neither. Writing the
message as a row *in the same transaction* gives that atomicity, and the worker's outbox
relay later copies unpublished rows to Kafka. Delivery is at least once (the relay can
crash between the broker ack and marking the row), so each message carries its outbox row
id in the ``rl-outbox-id`` header for consumers to deduplicate on.

A row whose publish fails is retried after a capped backoff (``available_at``). Without it,
rows that can never be published (a deleted topic, say) would be claimed first on every
round: retried in a hot loop and, once they fill a whole batch, starving every other row.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from rl_db.base import Base, UUIDPrimaryKeyMixin

MAX_TOPIC_LEN = 249  # Kafka's own limit
MAX_KEY_LEN = 512

# Headers the relay sets itself. A writer-supplied copy would travel next to the real one,
# and a consumer reading the first match would deduplicate on a forged id.
OUTBOX_ID_HEADER = "rl-outbox-id"
RESERVED_HEADERS = frozenset({OUTBOX_ID_HEADER, "content-type"})


class OutboxEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "outbox"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    topic: Mapped[str] = mapped_column(String(MAX_TOPIC_LEN), nullable=False)
    key: Mapped[str] = mapped_column(String(MAX_KEY_LEN), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    headers: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Earliest time the relay may (re)try this row: insert time, then pushed back on failure.
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_outbox_tenant_id_created_at", "tenant_id", "created_at"),
        # The relay's poll ("oldest unpublished first"). It is partial, so it stays as
        # small as the backlog no matter how many published rows accumulate.
        Index("ix_outbox_unpublished", "id", postgresql_where=text("published_at IS NULL")),
    )

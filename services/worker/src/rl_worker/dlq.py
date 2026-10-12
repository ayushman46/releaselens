"""Dead-letter messages: the original bytes plus enough metadata to triage and replay.

A DLQ message keeps the original **key and value unchanged**, so replay means producing it
back to ``original_topic``. It keeps the original headers too, and adds these:

=====================  ===========================================================
``error_type``         stable category (``malformed_json``, ``validation_error``, …)
``error_message``      human-readable reason, truncated to 1 000 characters
``stack``              formatted traceback, truncated to its *last* 8 000 characters
``original_topic``     where the message came from
``original_partition`` its partition
``original_offset``    its offset (with topic and partition, a unique address)
``failed_at``          ISO-8601 UTC time of the failure
=====================  ===========================================================

**Size bound.** The DLQ copy must always fit in one Kafka message. If it did not, the
DLQ write would fail on every attempt; the consumer classifies that as transient, so one
message just under the broker's size limit would pause its partition for ever. The copy is
therefore bounded: a value over :data:`MAX_DLQ_VALUE_BYTES`, a key over
:data:`MAX_DLQ_KEY_BYTES` or original headers over :data:`MAX_ORIGINAL_HEADER_BYTES` are cut,
and ``value_truncated`` / ``key_truncated`` / ``original_headers_truncated`` (``true``) plus
``original_value_bytes`` say so. Such a message could never be replayed anyway: every
event schema caps its own size far below these bounds (``MAX_ENVELOPE_BYTES`` is 64 KiB).
"""

from __future__ import annotations

import traceback
from datetime import datetime
from typing import Final

from confluent_kafka import Message

from rl_core.events import EventError
from rl_worker.kafka import OutgoingMessage

MAX_MESSAGE_CHARS: Final = 1000
MAX_STACK_CHARS: Final = 8000
# With these bounds the worst-case DLQ copy (value + key + original headers + metadata, the
# stack at 4 bytes per character) stays well under librdkafka's 1 000 000-byte default
# ``message.max.bytes``.
MAX_DLQ_VALUE_BYTES: Final = 512 * 1024
MAX_DLQ_KEY_BYTES: Final = 4 * 1024
MAX_ORIGINAL_HEADER_BYTES: Final = 64 * 1024


def _truncate_head(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _truncate_tail(text: str, limit: int) -> str:
    # The innermost frames, at the end of a traceback, are the useful ones.
    return text if len(text) <= limit else "…" + text[-(limit - 1) :]


def _utf8(text: str) -> bytes:
    # Error text can quote input. A lone surrogate there must not make the DLQ write raise
    # (that would crash the consumer on every redelivery), so it is escaped instead.
    return text.encode("utf-8", errors="backslashreplace")


def _as_bytes(value: str | bytes | None) -> bytes:
    if value is None:
        return b""
    return value if isinstance(value, bytes) else _utf8(value)


def dead_letter(
    msg: Message,
    *,
    dlq_topic: str,
    error: EventError,
    cause: BaseException,
    failed_at: datetime,
) -> OutgoingMessage:
    stack = "".join(traceback.format_exception(cause))
    markers: list[tuple[str, bytes]] = []

    # Original headers are kept, in order, until their total size would pass the bound. A
    # null header value becomes empty bytes.
    headers: list[tuple[str, bytes]] = []
    header_bytes = 0
    for header in msg.headers() or []:
        header_name, header_value = str(header[0]), _as_bytes(header[1])
        header_bytes += len(_utf8(header_name)) + len(header_value)
        if header_bytes > MAX_ORIGINAL_HEADER_BYTES:
            markers.append(("original_headers_truncated", b"true"))
            break
        headers.append((header_name, header_value))

    key = msg.key()
    if key is not None and len(_as_bytes(key)) > MAX_DLQ_KEY_BYTES:
        key = _as_bytes(key)[:MAX_DLQ_KEY_BYTES]
        markers.append(("key_truncated", b"true"))

    value = msg.value()
    if value is not None and len(value) > MAX_DLQ_VALUE_BYTES:
        markers += [
            ("value_truncated", b"true"),
            ("original_value_bytes", str(len(value)).encode()),
        ]
        value = value[:MAX_DLQ_VALUE_BYTES]

    headers += [
        ("error_type", error.error_type.encode()),
        ("error_message", _utf8(_truncate_head(str(error), MAX_MESSAGE_CHARS))),
        ("stack", _utf8(_truncate_tail(stack, MAX_STACK_CHARS))),
        ("original_topic", _utf8(str(msg.topic()))),
        ("original_partition", str(msg.partition()).encode()),
        ("original_offset", str(msg.offset()).encode()),
        ("failed_at", failed_at.isoformat().encode()),
        *markers,
    ]
    return OutgoingMessage(topic=dlq_topic, key=key, value=value, headers=headers)

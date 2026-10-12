"""Dead-letter message construction: original bytes kept, metadata headers added."""

from __future__ import annotations

from datetime import UTC, datetime

from rl_core.events import EventValidationError
from rl_worker.dlq import (
    MAX_DLQ_KEY_BYTES,
    MAX_DLQ_VALUE_BYTES,
    MAX_MESSAGE_CHARS,
    MAX_ORIGINAL_HEADER_BYTES,
    MAX_STACK_CHARS,
    dead_letter,
)


class _Msg:
    """Duck-typed stand-in for confluent_kafka.Message (which cannot be built in Python)."""

    def topic(self) -> str:
        return "deployment.events"

    def partition(self) -> int:
        return 4

    def offset(self) -> int:
        return 1234

    def key(self) -> bytes:
        return b"tenant:payment"

    def value(self) -> bytes:
        return b'{"broken": '

    def headers(self) -> list[tuple[str, bytes | None]]:
        return [("content-type", b"application/json"), ("empty", None)]


def _cause() -> EventValidationError:
    # A very long message makes both the error_message and the stack exceed their limits.
    try:
        raise EventValidationError("payload.commit_sha: " + "x" * 20_000 + " END")
    except EventValidationError as exc:
        return exc


def test_dead_letter_keeps_original_and_adds_metadata() -> None:
    cause = _cause()
    failed_at = datetime(2026, 10, 13, 12, 30, tzinfo=UTC)
    out = dead_letter(
        _Msg(),  # type: ignore[arg-type]
        dlq_topic="deployment.events.dlq",
        error=cause,
        cause=cause,
        failed_at=failed_at,
    )
    assert out.topic == "deployment.events.dlq"
    assert out.key == b"tenant:payment"
    assert out.value == b'{"broken": '
    headers = dict(out.headers)
    assert headers["content-type"] == b"application/json"
    assert headers["empty"] == b""
    assert headers["error_type"] == b"validation_error"
    assert headers["error_message"].startswith(b"payload.commit_sha")
    assert headers["original_topic"] == b"deployment.events"
    assert headers["original_partition"] == b"4"
    assert headers["original_offset"] == b"1234"
    assert headers["failed_at"] == b"2026-10-13T12:30:00+00:00"
    assert len(headers["error_message"].decode()) == MAX_MESSAGE_CHARS
    stack = headers["stack"].decode()
    assert len(stack) == MAX_STACK_CHARS
    assert stack.startswith("…")  # truncated from the front ...
    assert stack.rstrip().endswith("END")  # ... so the innermost part survives


class _HugeMsg(_Msg):
    """A message just under the broker limit: huge value, oversized key and headers."""

    def key(self) -> bytes:
        return b"k" * (MAX_DLQ_KEY_BYTES + 10)

    def value(self) -> bytes:
        return b"v" * 999_000

    def headers(self) -> list[tuple[str, bytes | None]]:
        return [("first", b"a" * 100), ("big", b"b" * MAX_ORIGINAL_HEADER_BYTES), ("last", b"c")]


def test_oversized_parts_are_bounded_and_flagged() -> None:
    """Without these bounds the DLQ copy of a near-limit message could never be written,
    and the consumer would retry (and pause) its partition for ever."""
    cause = _cause()
    out = dead_letter(
        _HugeMsg(),  # type: ignore[arg-type]
        dlq_topic="deployment.events.dlq",
        error=cause,
        cause=cause,
        failed_at=datetime(2026, 10, 13, tzinfo=UTC),
    )
    headers = dict(out.headers)
    assert out.value == b"v" * MAX_DLQ_VALUE_BYTES
    assert headers["value_truncated"] == b"true"
    assert headers["original_value_bytes"] == b"999000"
    assert out.key == b"k" * MAX_DLQ_KEY_BYTES
    assert headers["key_truncated"] == b"true"
    assert headers["first"] == b"a" * 100  # headers before the bound are kept, in order
    assert "big" not in headers
    assert "last" not in headers
    assert headers["original_headers_truncated"] == b"true"
    # The whole DLQ record stays under librdkafka's default 1 000 000-byte message limit.
    size = len(out.value or b"") + len(out.key or b"")
    size += sum(len(name.encode()) + len(value) for name, value in out.headers)
    assert size < 1_000_000
    # Every metadata header is still present.
    for name in (
        "error_type",
        "error_message",
        "stack",
        "original_topic",
        "original_partition",
        "original_offset",
        "failed_at",
    ):
        assert headers[name]


def test_small_messages_are_copied_unchanged_without_markers() -> None:
    cause = _cause()
    out = dead_letter(
        _Msg(),  # type: ignore[arg-type]
        dlq_topic="d",
        error=cause,
        cause=cause,
        failed_at=datetime(2026, 10, 13, tzinfo=UTC),
    )
    names = [name for name, _ in out.headers]
    assert not {"value_truncated", "key_truncated", "original_headers_truncated"} & set(names)
    assert names[:2] == ["content-type", "empty"]  # original headers first, in order


def test_unencodable_error_text_cannot_crash_the_dead_letter_path() -> None:
    """A lone surrogate quoted in an error message must be escaped, not raise: an exception
    here would crash the consumer on every redelivery of the same message."""
    try:
        raise EventValidationError("bad value \ud800 in labels")
    except EventValidationError as exc:
        cause = exc
    out = dead_letter(
        _Msg(),  # type: ignore[arg-type]
        dlq_topic="d",
        error=cause,
        cause=cause,
        failed_at=datetime(2026, 10, 13, tzinfo=UTC),
    )
    headers = dict(out.headers)
    assert headers["error_message"] == b"bad value \\ud800 in labels"
    assert b"\\ud800" in headers["stack"]

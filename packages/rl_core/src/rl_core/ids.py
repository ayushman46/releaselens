"""Identifier helpers.

Python 3.12's ``uuid`` module has no UUIDv7 (added in 3.14), so we implement RFC 9562 §5.7:
48-bit big-endian Unix epoch milliseconds, 4-bit version (0b0111), 12 bits ``rand_a``,
2-bit variant (0b10), 62 bits ``rand_b``.

Within a single process, IDs are strictly increasing: if the clock has not advanced (or went
backwards) since the previous call, ``rand_a`` is used as a 12-bit counter (RFC 9562 method 1).
When the counter overflows, the timestamp is advanced by one millisecond.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

_lock = threading.Lock()
_last_ms = -1
_counter = 0

_MAX_COUNTER = 0xFFF


def uuid7(*, now_ms: int | None = None) -> uuid.UUID:
    """Return a new, process-monotonic UUIDv7.

    ``now_ms`` exists for tests; production callers never pass it.
    """
    global _last_ms, _counter
    with _lock:
        ms = time.time_ns() // 1_000_000 if now_ms is None else now_ms
        if ms > _last_ms:
            _last_ms = ms
            # Start the counter in the lower half so a burst has room before overflowing.
            _counter = int.from_bytes(os.urandom(2), "big") & 0x7FF
        else:
            _counter += 1
            if _counter > _MAX_COUNTER:
                _last_ms += 1
                _counter = 0
            ms = _last_ms
        rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
        value = (ms & ((1 << 48) - 1)) << 80
        value |= 0x7 << 76
        value |= (_counter & _MAX_COUNTER) << 64
        value |= 0b10 << 62
        value |= rand_b
        return uuid.UUID(int=value)


def uuid7_timestamp_ms(value: uuid.UUID) -> int:
    """Extract the Unix-epoch millisecond timestamp embedded in a UUIDv7."""
    if value.version != 7:
        raise ValueError(f"not a UUIDv7: {value}")
    return value.int >> 80

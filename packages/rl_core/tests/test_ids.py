import uuid

import pytest

from rl_core.ids import uuid7, uuid7_timestamp_ms


def test_uuid7_has_version_and_variant_bits() -> None:
    value = uuid7()
    assert value.version == 7
    assert value.variant == uuid.RFC_4122


def test_uuid7_embeds_timestamp() -> None:
    value = uuid7(now_ms=1_700_000_000_123)
    assert uuid7_timestamp_ms(value) >= 1_700_000_000_123


def test_uuid7_is_strictly_monotonic_within_same_millisecond() -> None:
    ids = [uuid7(now_ms=1_800_000_000_000) for _ in range(5000)]  # forces counter overflow
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_uuid7_monotonic_when_clock_goes_backwards() -> None:
    a = uuid7(now_ms=1_900_000_000_500)
    b = uuid7(now_ms=1_900_000_000_100)
    assert b > a


def test_uuid7_timestamp_rejects_other_versions() -> None:
    with pytest.raises(ValueError, match="UUIDv7"):
        uuid7_timestamp_ms(uuid.uuid4())

"""Retry delays: capped exponential backoff with full jitter.

``delay(n)`` is uniform on ``[0, min(cap, base * 2**(n-1))]``: "full jitter" from the AWS
Architecture Blog's *Exponential Backoff and Jitter* (2015). Jitter spreads out the retries
of many consumers that failed at the same moment (e.g. all of them losing the database
together), so the recovering dependency is not hit by synchronized retry waves.

Full jitter can return a delay near zero. That is fine inside a bounded retry loop, but
waits that must never collapse into a tight loop (pausing a partition) use
:meth:`Backoff.pause_delay`, which keeps at least half of the cap.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

_MAX_EXPONENT = 62  # 2**62 already exceeds any sane cap; avoids huge ints and overflow


@dataclass(frozen=True, slots=True)
class Backoff:
    base_s: float = 0.5
    cap_s: float = 30.0
    max_attempts: int = 5
    rng: random.Random = field(
        default_factory=random.Random,
        compare=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if self.base_s <= 0:
            raise ValueError("base_s must be positive")
        if self.cap_s < self.base_s:
            raise ValueError("cap_s must be >= base_s")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")

    def ceiling(self, attempt: int) -> float:
        """Upper bound of the delay after the ``attempt``-th consecutive failure (1-based)."""
        if attempt < 1:
            raise ValueError("attempt is 1-based")
        exponent = min(attempt - 1, _MAX_EXPONENT)
        return float(min(self.cap_s, self.base_s * (2**exponent)))

    def delay(self, attempt: int) -> float:
        """Full-jitter delay after the ``attempt``-th consecutive failure."""
        return self.rng.uniform(0.0, self.ceiling(attempt))

    def pause_delay(self) -> float:
        """Delay before retrying after ``max_attempts`` quick retries all failed.

        Always within ``[cap/2, cap]``, so a dependency that stays down is probed at most
        about once per half-cap and never in a tight loop.
        """
        return self.rng.uniform(self.cap_s / 2, self.cap_s)

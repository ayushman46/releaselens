"""Feature definitions for the CI-failure risk model — the single source of truth (ADR-0006).

* :mod:`rl_core.features.schema`  — feature list, version, forbidden inputs
* :mod:`rl_core.features.history` — strictly-finished-before history windows (numpy, vectorised)
* :mod:`rl_core.features.change`  — change / test / codebase / process / time features
* :mod:`rl_core.features.offline` — batch computation over a Polars frame of build inputs
* :mod:`rl_core.features.online`  — per-request computation from a payload + tenant history

Offline and online both call the same pure functions; parity is tested.
"""

from rl_core.features.schema import (
    FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION,
    FORBIDDEN_INPUTS,
    HISTORY_FEATURES,
)

__all__ = ["FEATURE_NAMES", "FEATURE_SCHEMA_VERSION", "FORBIDDEN_INPUTS", "HISTORY_FEATURES"]

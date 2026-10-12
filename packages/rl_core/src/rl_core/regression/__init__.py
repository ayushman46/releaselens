"""Post-deployment regression engine (spec M8).

* :mod:`rl_core.regression.windows`  — :class:`WindowConfig` (all thresholds) and analysis windows
* :mod:`rl_core.regression.metrics`  — per-service request samples and window statistics
* :mod:`rl_core.regression.evidence` — JSON-serialisable evidence models and enums
* :mod:`rl_core.regression.detect`   — per-metric gates, service status, severity v1
* :mod:`rl_core.regression.onset`    — onset-time estimate (minute buckets vs. baseline p95 CI)
* :mod:`rl_core.regression.analysis` — :func:`analyse_deployment`, the orchestrating entry point

Everything is pure and deterministic for a given seed. Wording rule: the engine reports a
*probable origin*, never a cause, and severity is a deterministic score, not a probability.

This package ``__init__`` deliberately imports nothing: :mod:`rl_core.triage` and
:mod:`rl_core.blast_radius` import the evidence models, and ``analysis`` imports them back.
"""

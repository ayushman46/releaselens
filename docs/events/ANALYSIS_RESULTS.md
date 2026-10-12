# `analysis.results` event contract

- **Producer:** the worker's analysis job handler. It writes the `regression_analyses` row and an
  outbox row **in one transaction**. The outbox relay then publishes to Kafka at least once,
  with header `rl-outbox-id` for consumer dedup.
- **Consumers:** the notifier (Slack/email alerts), and later UI cache warmers.
- **Topic:** `analysis.results` (6 partitions, 7-day retention, ADR-0007).
  **Key:** `"<tenant_id>:<deployment_id>"` (`rl_core.events.analysis.analysis_key`).
- **Schema:** `rl_core.events.analysis.AnalysisEnvelope` (strict: unknown fields rejected,
  64 KiB cap).

```json
{
  "schema_version": 1,
  "event_id": "0192…uuid7",
  "tenant_id": "…uuid…",
  "occurred_at": "2026-10-13T02:10:00Z",
  "type": "analysis.completed",
  "payload": {
    "deployment_id": "…", "analysis_id": "…", "kind": "final",
    "service": "payment", "environment": "demo", "version": "v2", "previous_version": "v1",
    "status": "REGRESSED", "severity_score": 58.6, "severity_band": "HIGH",
    "probable_origin": "payment", "confidence": "HIGH",
    "affected_services": ["orders", "gateway"],
    "engine_version": "regression-engine/1.0.0",
    "completed_at": "2026-10-13T02:10:00Z"
  }
}
```

- `status` is the deployed service's status (`HEALTHY | WARNING | REGRESSED |
  INSUFFICIENT_DATA`). `severity_score` is the deterministic severity v1 score (not a
  probability). `probable_origin` is null when confidence is LOW or the top candidates tie
  (ADR-0018).
- Consumers must be idempotent per `(tenant_id, deployment_id, kind)`. Early kinds (`early_5m`,
  `early_10m`) may be followed by a `final` that supersedes them.

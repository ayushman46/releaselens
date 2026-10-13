# Telemetry: OTLP ingest, `telemetry.spans`, span storage and analysis input

Status: built in M7 (ingest, consumer, storage) and M8 (analysis job handlers). Every number in
this document was measured (see [Measured throughput](#measured-throughput)) or is a configured
limit.

```
OTel SDK → Collector ──POST /v1/traces──▶ API ──telemetry.spans──▶ worker SpanConsumer
                     (protobuf|JSON, gzip)      (one msg / service)     │ one transaction / batch
                                                                       ▼
                         spans (daily partitions) + service_edges + service_minute_rollups
                                                                       │
             analysis_jobs (T+5, T+10, T+15) ──▶ AnalysisJobHandler ──▶ regression_analyses
                                                                     + outbox → analysis.results
```

## 1. Ingest contract: `POST /v1/traces`

| Aspect | Contract |
|---|---|
| Auth | API key in `x-releaselens-key` (what the Collector's `headers:` sends) or `Authorization: Bearer`. Scope **`telemetry:write`**. Missing/invalid key → `401`, key without the scope → `403` (ADR-0016). |
| Tenant | **Only** from the API key. Resource or span attributes (`tenant_id`, `releaselens.tenant_id`, …) are never read for it. |
| `Content-Type` | `application/x-protobuf` (also `application/protobuf`): binary `ExportTraceServiceRequest`. `application/json`: OTLP/JSON. Parameters (`; charset=utf-8`) are ignored. Anything else → `415`. |
| OTLP/JSON | The protobuf JSON mapping with OTLP's exceptions: `traceId` / `spanId` / `parentSpanId` are **hex**; 64-bit integers may be strings or numbers; enums are integers (names tolerated). lowerCamelCase and snake_case field names are both accepted; unknown fields are ignored. `links` and `events` are dropped before parsing (not stored). |
| `Content-Encoding` | `gzip` (multi-member allowed) or `identity`/absent. Anything else → `415`. |
| Size limits | Body as received ≤ **8 MiB** (`RL_OTLP_MAX_BODY_BYTES`), checked against `Content-Length` and while streaming. Decompressed ≤ **16 MiB** (`RL_OTLP_MAX_DECOMPRESSED_BYTES`): inflation stops as soon as the output passes the cap, so a gzip bomb costs at most 16 MiB of memory. Either → `413`. |
| Undecodable body | Not protobuf / not JSON / not an `ExportTraceServiceRequest`, invalid gzip → `400 invalid_otlp`. |
| Success | `200` with an `ExportTraceServiceResponse` in the request's encoding. When spans were rejected it carries `partial_success.rejected_spans` and an `error_message` ("2 span(s): end time before start time; …"). A request whose spans are *all* rejected is still `200` + `partial_success` (retrying cannot fix the data). |
| Kafka down | `503 event_bus_unavailable` + `Retry-After: 5`. The API answers 200 only after the broker acknowledged **every** message of the request (`acks=all`, idempotent producer). |
| Error bodies | The API's JSON envelope `{"error": {"code", "message", "request_id"}}` for 400/401/403/413/415/503. |

### Span mapping (`rl_core.events.spans.WireSpan`)

| ReleaseLens field | OTLP source |
|---|---|
| `service` | resource `service.name`; absent → `unknown_service` (the OTel SDK default). Must match `^[A-Za-z0-9][A-Za-z0-9._:/@-]*$`, ≤ 200 chars. |
| `service_version` | resource `service.version` (printable ASCII, ≤ 200) |
| `environment` | resource `deployment.environment.name`, else the older `deployment.environment` (`^[A-Za-z0-9][A-Za-z0-9._-]*$`, ≤ 64); absent → `null` |
| `trace_id`, `span_id`, `parent_span_id` | lowercase hex of the 16 / 8 / 8 id bytes |
| `kind` | `SPAN_KIND_UNSPECIFIED`/`INTERNAL` → `INTERNAL`; `SERVER`, `CLIENT`, `PRODUCER`, `CONSUMER` |
| `status_code` | `STATUS_CODE_ERROR` → `ERROR`, `OK` → `OK`, otherwise `UNSET` |
| `start_ns`, `end_ns` | `start_time_unix_nano`, `end_time_unix_nano` (integers, never floats) |
| `http_route` | span attribute `http.route` (≤ 512 chars) |
| `http_status` | `http.response.status_code`, else legacy `http.status_code` (int, numeric string or integral double); outside 100–599 → `null` |
| `attributes` | span attributes, bounded: ≤ 64 entries; keys ≤ 128 chars (longer keys dropped); strings truncated to 256 chars; arrays of scalars truncated to 16 items; nested maps, bytes and non-finite doubles dropped; ≤ 16 KiB of keys + values per span. NUL characters (which Postgres text/jsonb cannot hold) become U+FFFD. |

### Rejected spans (reported via `partial_success`; the rest of the request is accepted)

* trace id not 16 bytes or all zeros; span id not 8 bytes or all zeros; parent id not 8 bytes,
  all zeros, or equal to the span id;
* start time 0, or a time above 2⁶³−1; end before start;
* start more than **1 day** after the server clock (`RL_OTLP_MAX_FUTURE_SKEW_S`), or more than
  **7 days** before it (`RL_OTLP_MAX_SPAN_AGE_S`, the default span retention: older spans would
  land in a partition retention is about to drop);
* unknown span kind value;
* every span of a resource whose `service.name`, `service.version` or environment is invalid.

### Kafka production

Accepted spans are grouped by service. Each service becomes **one** `telemetry.spans` message,
or several when its spans would exceed 768 KiB encoded (`MAX_SPAN_BATCH_BYTES`; librdkafka's
default `message.max.bytes` is 1 000 000). Key: `"<tenant_id>:<service.name>"`
(`rl_core.events.topics.span_key`), so one service's batches stay ordered on one partition.
Headers: `content-type: application/json`, `rl-event-type: telemetry.span_batch`,
`rl-schema-version: 1`. The API producer uses lz4 compression.

A Collector retry after a `503` (or a timeout) may resend spans that were in fact written; the
consumer stores each span once (§3).

## 2. `telemetry.spans` message schema (v1)

```json
{
  "schema_version": 1,
  "type": "telemetry.span_batch",
  "batch_id": "0192…uuid7",
  "tenant_id": "…uuid…",
  "service": "payment",
  "received_at": "2026-10-13T12:00:00.123456+00:00",
  "spans": [
    {"trace_id": "4bf92f3577b34da6a3ce929d0e0e4736", "span_id": "00f067aa0ba902b7",
     "parent_span_id": null, "service_version": "v2.18", "environment": "demo",
     "name": "/charge", "kind": "SERVER", "start_ns": 1760356800000000000,
     "end_ns": 1760356800061000000, "status_code": "UNSET", "http_route": "/charge",
     "http_status": 200, "attributes": {"http.request.method": "POST"}}
  ]
}
```

Strict: unknown fields are rejected, NaN/∞ rejected, ≤ 20 000 spans and ≤ 768 KiB per message.
Parsing (`parse_span_batch`) raises the permanent-error taxonomy of `rl_core.events.errors`
(`malformed_json`, `unknown_event_type`, `unknown_schema_version`, `validation_error`,
`event_too_large`), which becomes the DLQ `error_type` header.

## 3. Span consumer (`rl_worker.consumers.spans.SpanConsumer`, group `rl-spans`)

Per poll, up to `RL_SPANS_BATCH_MESSAGES` (default 100) messages are handled as one unit:

1. parse every envelope; permanent failures → `telemetry.spans.dlq` (original key/value/headers
   plus `error_type`, `error_message`, `stack`, `original_topic/partition/offset`, `failed_at`);
2. check the envelopes' tenants exist (one query); unknown → DLQ `unknown_tenant` (`spans` has
   no FK to `organizations`: that would be a per-row check on the hottest insert path);
3. create missing day partitions, in a short transaction of its own;
4. **one transaction**: per tenant, `INSERT … SELECT FROM unnest(<arrays>) ON CONFLICT DO
   NOTHING RETURNING`, then — from the **returned (newly inserted) rows only** — service-edge
   calls and minute rollups, merged by `INSERT … ON CONFLICT DO UPDATE`;
5. commit the offsets (synchronously) after the database commit.

**Idempotency.** The span primary key is `(tenant_id, trace_id, span_id, start_time)`. A
redelivered batch inserts nothing, so it adds no edge call and no rollup count (tested: the same
messages twice leave spans, edge `call_count` and rollups unchanged).

**Edges.** `A → B` gains one call per span of `B` whose parent (same trace) is a span of
`A ≠ B`. Parent and child may arrive in different batches in either order. After the insert, the
consumer loads every stored span of the batch's traces (±1 day: `TRACE_LINK_WINDOW`) and counts
(a) each new span whose parent is stored, and (b) each *earlier-stored* child of a new span. Each
pair is counted exactly once, when the second of the two is stored. Two concurrent transactions
(one with the parent, one with the child) would each miss the other's uncommitted row, so
derivation takes a **per-tenant transaction advisory lock** before the trace-view query; the
second transaction then sees the first one's committed span (tested with both inserts done before
either derivation).

**Failures.** Transient (connection loss, timeouts, deadlocks, an unacknowledged DLQ write):
retry the batch with full-jitter backoff; after `RL_RETRY_MAX_ATTEMPTS`, seek every partition of
the batch back to its first offset, pause it, resume after `pause_delay()` (tested with a TCP
proxy cutting Postgres: nothing committed during the outage, everything stored exactly once
after it). A permanent error in the batch transaction (a value Postgres refuses) re-runs the batch
one message per transaction, so only the offending message is dead-lettered. A missing partition
(dropped by retention after being cached) clears the partition cache and is retried.

## 4. Storage (migration `0003_telemetry`)

### `spans` — declaratively partitioned by day

| Column | Type | Note |
|---|---|---|
| `tenant_id` | uuid | from the envelope (= the API key's tenant) |
| `trace_id` / `span_id` / `parent_span_id` | varchar(32) / varchar(16) / varchar(16) | lowercase hex |
| `service`, `service_version`, `environment` | varchar(200/200/64) | `environment` NULL when not reported |
| `name` | varchar(512) | |
| `kind` | varchar(16) | CHECK: SERVER, CLIENT, INTERNAL, PRODUCER, CONSUMER |
| `start_ns`, `end_ns` | bigint | CHECK `end_ns >= start_ns` |
| `duration_ms` | double precision | `(end_ns - start_ns) / 1e6` |
| `status_code` | varchar(8) | CHECK: UNSET, OK, ERROR |
| `http_route`, `http_status` | varchar(512), smallint | |
| `attributes` | jsonb | bounded (§1) |
| `start_time` | timestamptz | **partition key**: `start_ns` truncated to microseconds |

* `PARTITION BY RANGE (start_time)`; partition `spans_pYYYYMMDD` holds `[day 00:00 UTC, next day
  00:00 UTC)`. There is **no DEFAULT partition**: attaching a day would otherwise scan it.
* Primary key `(tenant_id, trace_id, span_id, start_time)` — the partition key must be part of it.
  Its leading columns serve the `(tenant_id, trace_id)` trace lookups, so no separate index exists
  for them (one fewer index on the insert path).
* Index `ix_spans_tenant_env_service_start_time (tenant_id, environment, service, start_time)`:
  the analysis window query and the catalog's "latest version seen".
* Alembic cannot express `PARTITION BY`: the parent is raw DDL, the ORM model mirrors it, and the
  models-equal-migrations test compares them. Runtime partitions are excluded from autogenerate by
  an `include_object` hook (`rl_db.admin.partitions.autogenerate_include_object`); a separate test
  inspects the catalog (`relkind = 'p'`, `RANGE (start_time)`, PK, index, bounds).

**Partition management** (`rl_db.admin.partitions`, idempotent, safe concurrently — advisory lock):
`ensure_partitions(days_ahead=2)` creates today + 2 days (UTC); `ensure_partitions_for_days(days)`
is what the consumer calls for days it has not seen; `drop_partitions_older_than(days)` drops a
partition once its **end** is at or before `now - days`. The worker's maintenance loop runs both
hourly (`RL_MAINTENANCE_INTERVAL_S`), with `RL_SPAN_RETENTION_DAYS` = 7 and
`RL_PARTITION_DAYS_AHEAD` = 2, and deletes minute rollups older than
`RL_ROLLUP_RETENTION_DAYS` = 30.

### `service_edges`

`(tenant_id, environment, caller_service, callee_service)` primary key; `call_count` (cumulative,
> 0), `first_seen`, `last_seen` (the callee span's start). `environment` is the callee span's,
else the caller's, else `''` ("not reported": `''` is never a valid environment name, and a
primary-key column cannot be NULL). FK to `organizations` (cascade).

### `service_minute_rollups` (SERVER spans only)

`(tenant_id, environment, service, minute)` primary key; `request_count`, `error_count`
(`Span.is_error`: status ERROR or HTTP 5xx on the SERVER span), `duration_sum_ms`,
`duration_max_ms`, `latency_buckets bigint[42]`. Merging is addition (counts, sums, element-wise
histogram), `GREATEST` for the max.

**Histogram buckets** (`rl_core.telemetry.rollup.LATENCY_BUCKET_BOUNDS_MS`): 41 inclusive upper
bounds `2^(k/2)` ms for `k = -6 … 34`, a factor √2 apart, plus one overflow bucket:

```
0.125, 0.1768, 0.25, 0.3536, 0.5, 0.7071, 1, 1.414, 2, 2.828, 4, 5.657, 8, 11.31, 16, 22.63,
32, 45.25, 64, 90.51, 128, 181.0, 256, 362.0, 512, 724.1, 1024, 1448, 2048, 2896, 4096, 5793,
8192, 11585, 16384, 23170, 32768, 46341, 65536, 92682, 131072 ms, then (131072, ∞)
```

Bucket `i` counts `bounds[i-1] < d ≤ bounds[i]` (bucket 0 includes 0). The bounds are part of the
stored schema: changing them needs a migration. `approx_quantile` returns the bucket that must
contain the percentile (`[low, high]`, at most √2 apart) and a geometric interpolation inside it
(measured on 5 000 lognormal latencies around 60 ms: p95 within 15 %). **Rollups are for display
only**; the regression engine always computes exact percentiles from raw spans.

### `regression_analyses`

`id` uuid7, `tenant_id`, `deployment_id` (composite FK → `deployments(tenant_id, id)`, cascade),
`kind` (the `analysis_job_kind` enum: `early_5m < early_10m < final`), `status` (overall verdict),
`severity_score` / `severity_band` (severity v1, not a probability), `probable_origin`,
`confidence`, `result` jsonb (the complete `RegressionAnalysis` of `engine_version`), `windows`
jsonb (profile and resolved windows, including `as_of`), `sampling` jsonb (span load counts and
sample), `created_at`, `updated_at`. `UNIQUE (tenant_id, deployment_id, kind)` — deployment ids are
globally unique, so this is the requested uniqueness per `(deployment, kind)` with `tenant_id`
leading (ADR-0010). Index `(tenant_id, created_at)`.

## 5. Analysis job handler (`rl_worker.analysis.AnalysisJobHandler`)

Registered for `early_5m`, `early_10m`, `final` (the job runner now always starts).

* **Windows.** Spec defaults (baseline 30 min, warm-up 2 min, post window to T+15 min) unless the
  deployment's environment has a profile in `RL_ANALYSIS_WINDOWS`. Default profile:
  `{"demo": {"baseline_minutes": 10, "warmup_minutes": 1, "post_end_minutes": 8}}`. A profile
  whose post window ends after the final job (`RL_ANALYSIS_POST_WINDOW_END_S`, 900 s) is refused at
  startup. The profile name and resolved windows are stored in `windows`.
* **`as_of`.** Early kinds analyse as of their **scheduled** time (`deployed_at + 5 / 10 min`,
  never later than the actual run), so a late or retried run still answers "what did we know at
  T+5". `final` analyses the full window and raises `AnalysisNotDueError` (retried by the runner)
  if it runs before the window has closed.
* **Span load** (`rl_db.repositories.analyses.load_analysis_spans`): the tenant's spans of the
  deployment's environment **and** spans without an environment (the engine keeps those), all
  kinds, start in `[baseline start, effective post end + 2 min]` (the tail keeps children of
  requests that start just before the end). Above `RL_ANALYSIS_MAX_SPANS` (200 000) whole traces
  are sampled deterministically: keep a trace when the first 32 bits of `md5(trace_id)` are below
  `max_spans / matched · 2³²`. Counts and throughput then shrink by the recorded
  `sample_fraction`.
* `analyse_deployment` runs in a worker thread. Then **one transaction** upserts the row and adds
  the `analysis.results` outbox message (`rl_core.events.analysis.AnalysisEnvelope`, key
  `tenant_id:deployment_id`, see [ANALYSIS_RESULTS.md](ANALYSIS_RESULTS.md)). The envelope's
  `status` is the deployed service's own status, as that contract specifies; the row's `status`
  is the overall verdict of the analysis.

## 6. Read APIs (scope `read`, tenant-scoped, 404 across tenants)

| Endpoint | Returns |
|---|---|
| `GET /api/v1/deployments/{id}/analysis` | every analysis of the deployment (most complete kind first) and `latest` (`final` > `early_10m` > `early_5m`) |
| `GET /api/v1/regressions?environment=&service=&limit=&cursor=` | deployments whose latest analysis is REGRESSED or WARNING: severity, probable origin, confidence, affected services (blast radius); keyset-paginated by `(deployed_at, id)` |
| `GET /api/v1/services?environment=&limit=&cursor=` | known services (rollups in the last 7 days, deployments, edges): latest version seen (spans, last 24 h), last deployment, and over the **last 15 complete minutes**: requests, rate/min, errors, error rate, mean/max latency and `latency_p95_ms_approx` with its bucket bounds (`latency_approximate: true`) |
| `GET /api/v1/service-map?environment=&window_hours=24` | nodes and edges (cumulative `call_count`, edges last seen in the window, ≤ 5 000), per-node `status` / `impact` / `probable_origin` from the environment's latest analysis |

## Measured throughput

`services/worker/tests/test_span_consumer.py::test_throughput_with_20k_generated_spans`: 21 615
spans of the demo topology (`tracegen` control scenario, 26 minutes at 60 requests/min) in 47
messages of ≤ 512 spans (one Collector batch each), pre-produced to a 12-partition topic, one
consumer. "Busy" is the time spent handling batches (parse, tenant check, partitions, insert,
edge/rollup derivation, transaction and offset commits); "end to end" also includes the consumer
group join.

| Batch (`batch_messages`) | Spans per transaction | Busy throughput | End to end |
|---|---|---|---|
| 100 (1 transaction) | 21 615 | **17 000 – 25 000 spans/s** (3 runs: 25.0k, 17.0k, 17.3k) | 4 700 – 14 100 spans/s |
| 8 (6 transactions) | ≈ 3 600 | **11 000 – 11 900 spans/s** (2 runs) | 3 600 – 4 100 spans/s |

Hardware: Apple M4 host (10 cores, 16 GB) running the consumer in CPython 3.12; Postgres
16.15-alpine (default configuration, testcontainers) and `apache/kafka-native` 4.3.1 in a Colima
VM with **2 vCPU / 4 GiB** shared with other containers. Numbers vary by up to ~30 % between
runs on this shared VM. They are single-consumer figures, not a capacity limit: partitions allow
more consumer instances. ADR-0008's ClickHouse revisit threshold is still to be measured with k6
in M12.

The end-to-end test (`test_telemetry_e2e.py`) pushes ≈ 37 700 spans through `POST /v1/traces`
(26 gzip protobuf requests of 1 500 spans) in ≈ 1–1.5 s, stores them in ≈ 3–6 s (including the
group join) and runs the three analysis jobs in ≈ 3 s.

## Known limitations

* The final job runs at `T + RL_ANALYSIS_POST_WINDOW_END_S` for every environment; a shorter
  profile (demo: 8 min) is analysed fully by `early_10m` and again by `final` at T+15.
* Spans arriving after the final analysis (ingest lag beyond T+15) are not re-analysed.
* Parent and child spans more than 1 day apart are not linked into an edge.
* `spans` has no FK to `organizations`: deleting a tenant leaves its spans until retention drops
  their partitions (a tenant purge job is future work).
* The span insert deduplicates on the primary key including `start_time`; a re-export of the
  same span with a *different* start time is stored twice.

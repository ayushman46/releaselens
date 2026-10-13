/**
 * Test-only fixture in the shape of rl_core.regression.analysis.RegressionAnalysis (engine 1.x),
 * the JSON the API returns in AnalysisOut.result. Never imported by application code.
 */
import type { AnalysisOut, LatencyEvidence, RegressionAnalysisResult, ServiceEvidence } from "@/lib/api/types";

const gate = (name: string, passed: boolean, observed: number, threshold: number) => ({ name, passed, observed, threshold, op: ">=" as const });

function latency(percentile: number, base: number, post: number, low: number, high: number, regressed: boolean): LatencyEvidence {
  return {
    metric: "latency",
    percentile,
    baseline_ms: base,
    post_ms: post,
    abs_delta_ms: post - base,
    rel_delta: (post - base) / base,
    ci_low_ms: low,
    ci_high_ms: high,
    ci_confidence: 0.95,
    bootstrap_resamples: 1000,
    bootstrap_seed: 0,
    n_baseline: 6012,
    n_post: 4980,
    gates: [gate("relative_increase", regressed, (post - base) / base, 0.25)],
    regressed,
  };
}

function evidence(service: string, overrides: Partial<ServiceEvidence> = {}): ServiceEvidence {
  return {
    service,
    variant: "strict",
    status: "HEALTHY",
    sample_gate: { baseline_requests: 6012, post_requests: 4980, min_requests: 200, passed: true },
    baseline: { requests: 6012, errors: 6, error_rate: 0.001, throughput_rpm: 600.2, latency_p50_ms: 40, latency_p95_ms: 80, latency_p99_ms: 120, self_time_p95_ms: 30 },
    post: { requests: 4980, errors: 5, error_rate: 0.001, throughput_rpm: 553.3, latency_p50_ms: 41, latency_p95_ms: 82, latency_p99_ms: 121, self_time_p95_ms: 31 },
    latency: [],
    self_time: [],
    error_rate: null,
    own_error_rate: null,
    throughput: null,
    severity: { formula: "severity-v1", score: 0, band: "HEALTHY", latency_component: 0, error_component: 0 },
    onset: null,
    regressed_metrics: [],
    self_time_regressed: false,
    originates_errors: false,
    ...overrides,
  };
}

export const paymentEvidence = evidence("payment", {
  status: "REGRESSED",
  baseline: { requests: 6012, errors: 12, error_rate: 0.002, throughput_rpm: 600.2, latency_p50_ms: 55, latency_p95_ms: 92.5, latency_p99_ms: 130, self_time_p95_ms: 40 },
  post: { requests: 4980, errors: 184, error_rate: 0.0369, throughput_rpm: 553.3, latency_p50_ms: 300, latency_p95_ms: 342.5, latency_p99_ms: 410, self_time_p95_ms: 290 },
  latency: [latency(0.5, 55, 300, 238, 252, true), latency(0.95, 92.5, 342.5, 210, 290, true), latency(0.99, 130, 410, 250, 310, true)],
  error_rate: {
    metric: "error_rate",
    baseline_errors: 12,
    baseline_requests: 6012,
    post_errors: 184,
    post_requests: 4980,
    baseline_rate: 0.002,
    post_rate: 0.0369,
    abs_delta_pp: 3.49,
    rel_delta: 17.45,
    z: 12.1,
    p_value: 1e-30,
    exact_p_value: 2e-31,
    gates: [gate("abs_increase_pp", true, 3.49, 0.5)],
    regressed: true,
  },
  throughput: { baseline_rpm: 600.2, post_rpm: 553.3, rel_change: -0.078, gate: gate("throughput_drop", false, 0.078, 0.3), dropped: false },
  severity: { formula: "severity-v1", score: 58.6, band: "HIGH", latency_component: 35, error_component: 23.6 },
  onset: {
    onset_at: "2026-10-13T02:01:00Z",
    minutes_after_deploy: 1,
    baseline_p95_ms: 92.5,
    baseline_p95_ci_high_ms: 101,
    ci_confidence: 0.95,
    bootstrap_resamples: 1000,
    bootstrap_seed: 0,
    bucket_seconds: 60,
    min_samples_per_bucket: 20,
    buckets: [
      { start: "2026-10-13T02:00:00Z", samples: 510, p95_ms: 99, exceeds: false },
      { start: "2026-10-13T02:01:00Z", samples: 498, p95_ms: 340, exceeds: true },
      { start: "2026-10-13T02:02:00Z", samples: 12, p95_ms: null, exceeds: false },
    ],
  },
  regressed_metrics: ["latency_p95", "error_rate"],
  self_time_regressed: true,
  originates_errors: true,
});

export const analysisResult: RegressionAnalysisResult = {
  engine_version: "regression-engine/1.0.0",
  deployment: { service: "payment", environment: "demo", version: "v2", previous_version: "v1", deployed_at: "2026-10-13T02:00:00Z" },
  windows: {
    deployed_at: "2026-10-13T02:00:00Z",
    baseline_start: "2026-10-13T01:50:00Z",
    baseline_end: "2026-10-13T02:00:00Z",
    post_start: "2026-10-13T02:01:00Z",
    post_end: "2026-10-13T02:08:00Z",
    effective_post_end: "2026-10-13T02:08:00Z",
    as_of: null,
  },
  status: "REGRESSED",
  severity: paymentEvidence.severity,
  severity_service: "payment",
  services: [
    evidence("gateway", { status: "REGRESSED" }),
    evidence("inventory"),
    evidence("orders", { status: "REGRESSED" }),
    paymentEvidence,
  ],
  relaxed_services: [],
  graph: {
    nodes: ["gateway", "inventory", "orders", "payment", "fraud"],
    edges: [
      { source: "gateway", target: "orders", call_count: 9000 },
      { source: "orders", target: "payment", call_count: 8000 },
      { source: "orders", target: "inventory", call_count: 8000 },
      { source: "payment", target: "fraud", call_count: 7000 },
    ],
  },
  triage: {
    candidates: [
      {
        service: "payment",
        score: 7,
        onset_at: "2026-10-13T02:01:00Z",
        factors: [{ factor: "deployed", applies: true, points: 3, reason: "payment was deployed" }],
        reasoning: ["payment was the deployed service", "payment's own time (self-time) p95 regressed", "payment originates the new errors"],
      },
      { service: "orders", score: 2, onset_at: null, factors: [], reasoning: ["orders calls a degraded service"] },
    ],
    confidence: "HIGH",
    probable_origin: "payment",
    shortlist: ["payment"],
    top_score: 7,
    margin: 5,
    weights: { deployed: 3 },
    summary: "payment leads with 7 points, 5 ahead of orders.",
  },
  blast_radius: {
    origin: "payment",
    nodes: [
      { service: "orders", direction: "upstream", depth: 1, impact: "AFFECTED_UPSTREAM", strict_status: "REGRESSED", relaxed_status: null, affected_via: "strict" },
      { service: "gateway", direction: "upstream", depth: 2, impact: "AFFECTED_UPSTREAM", strict_status: "REGRESSED", relaxed_status: null, affected_via: "strict" },
      { service: "fraud", direction: "downstream", depth: 1, impact: "UNKNOWN_DOWNSTREAM", strict_status: "INSUFFICIENT_DATA", relaxed_status: null, affected_via: null },
    ],
    affected: ["orders", "gateway"],
    not_in_radius: ["inventory"],
  },
  input: { spans_received: 40000, other_environment_spans_dropped: 0, duplicate_spans_dropped: 3, spans_in_windows: 39000 },
};

export const analysisOut: AnalysisOut = {
  id: "0192a000-0000-7000-8000-000000000001",
  deployment_id: "0192a000-0000-7000-8000-000000000002",
  kind: "final",
  status: "REGRESSED",
  severity_score: 58.6,
  severity_band: "HIGH",
  probable_origin: "payment",
  confidence: "HIGH",
  engine_version: "regression-engine/1.0.0",
  windows: { ...analysisResult.windows },
  sampling: {},
  created_at: "2026-10-13T02:10:00Z",
  updated_at: "2026-10-13T02:10:00Z",
  result: analysisResult as unknown as Record<string, unknown>,
};

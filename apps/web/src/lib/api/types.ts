import type { components } from "@/lib/api/schema";

type S = components["schemas"];

export type MeOut = S["MeOut"];
export type UserOut = S["UserOut"];
export type MembershipOut = S["MembershipOut"];
export type OrgSummary = S["OrgSummary"];
export type OrgOut = S["OrgOut"];
export type SessionOut = S["SessionOut"];
export type Role = S["Role"];
export type MemberOut = S["MemberOut"];
export type InviteOut = S["InviteOut"];
export type ProjectOut = S["ProjectOut"];
export type ApiKeyOut = S["ApiKeyOut"];
export type ApiKeyCreated = S["ApiKeyCreated"];
export type AuditEntryOut = S["AuditEntryOut"];
export type AuditPage = S["AuditPage"];
export type AlertRuleOut = S["AlertRuleOut"];
export type BillingOut = S["BillingOut"];
export type PlanLimitsOut = S["PlanLimitsOut"];
export type DeploymentOut = S["DeploymentOut"];
export type DeploymentDetail = S["DeploymentDetail"];
export type DeploymentPage = S["DeploymentPage"];
export type DeploymentAnalysis = S["DeploymentAnalysis"];
export type AnalysisOut = S["AnalysisOut"];
export type RegressionItem = S["RegressionItem"];
export type RegressionPage = S["RegressionPage"];
/** openapi-fetch widens the `[low, high]` tuple to an array in response data; mirror that. */
export type ServiceHealth = Omit<S["ServiceHealth"], "latency_p95_bucket_ms"> & {
  latency_p95_bucket_ms: Array<number | null> | null;
};
export type ServiceItem = Omit<S["ServiceItem"], "health"> & { health: ServiceHealth };
export type ServicePage = { items: ServiceItem[]; next_cursor: string | null };
export type ServiceMap = S["ServiceMap"];
export type ServiceMapNode = S["ServiceMapNode"];
export type ServiceMapEdge = S["ServiceMapEdge"];

export type AnalysisStatus = AnalysisOut["status"];
export type Confidence = NonNullable<AnalysisOut["confidence"]>;

/* ----------------------------------------------------------------------------------------- */
/* AnalysisOut.result — the RegressionAnalysis JSON of engine_version regression-engine/1.x.  */
/* The OpenAPI schema types it as `{[key: string]: unknown}`, so these interfaces mirror      */
/* rl_core.regression.{analysis,evidence,windows}, rl_core.triage and rl_core.blast_radius.   */
/* `parseAnalysisResult` checks the parts the UI reads before trusting them.                 */
/* ----------------------------------------------------------------------------------------- */

export type ServiceStatus = "HEALTHY" | "WARNING" | "REGRESSED" | "INSUFFICIENT_DATA";
export type SeverityBandName = "HEALTHY" | "WARNING" | "HIGH" | "CRITICAL";

export interface Gate {
  name: string;
  passed: boolean;
  observed: number | null;
  threshold: number;
  op: ">=" | ">" | "<" | "<=";
  note?: string | null;
}

export interface WindowSummary {
  requests: number;
  errors: number;
  error_rate: number | null;
  throughput_rpm: number | null;
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
  latency_p99_ms: number | null;
  self_time_p95_ms: number | null;
}

export interface LatencyEvidence {
  metric: "latency" | "self_time";
  percentile: number;
  baseline_ms: number;
  post_ms: number;
  abs_delta_ms: number;
  rel_delta: number | null;
  ci_low_ms: number;
  ci_high_ms: number;
  ci_confidence: number;
  bootstrap_resamples: number;
  bootstrap_seed: number;
  n_baseline: number;
  n_post: number;
  gates: Gate[];
  regressed: boolean;
}

export interface ErrorRateEvidence {
  metric: "error_rate" | "own_error_rate";
  baseline_errors: number;
  baseline_requests: number;
  post_errors: number;
  post_requests: number;
  baseline_rate: number;
  post_rate: number;
  abs_delta_pp: number;
  rel_delta: number | null;
  z: number;
  p_value: number;
  exact_p_value?: number | null;
  gates: Gate[];
  regressed: boolean;
}

export interface ThroughputEvidence {
  baseline_rpm: number | null;
  post_rpm: number | null;
  rel_change: number | null;
  gate: Gate;
  dropped: boolean;
}

export interface OnsetBucket {
  start: string;
  samples: number;
  p95_ms: number | null;
  exceeds: boolean;
}

export interface OnsetEvidence {
  onset_at: string | null;
  minutes_after_deploy: number | null;
  baseline_p95_ms: number;
  baseline_p95_ci_high_ms: number;
  ci_confidence: number;
  bootstrap_resamples: number;
  bootstrap_seed: number;
  bucket_seconds: number;
  min_samples_per_bucket: number;
  buckets: OnsetBucket[];
}

export interface Severity {
  formula: string;
  score: number;
  band: SeverityBandName;
  latency_component: number;
  error_component: number;
}

export interface SampleGate {
  baseline_requests: number;
  post_requests: number;
  min_requests: number;
  passed: boolean;
}

export interface ServiceEvidence {
  service: string;
  variant: string;
  status: ServiceStatus;
  sample_gate: SampleGate;
  baseline: WindowSummary;
  post: WindowSummary;
  latency: LatencyEvidence[];
  self_time: LatencyEvidence[];
  error_rate: ErrorRateEvidence | null;
  own_error_rate: ErrorRateEvidence | null;
  throughput: ThroughputEvidence | null;
  severity: Severity | null;
  onset: OnsetEvidence | null;
  regressed_metrics: string[];
  self_time_regressed: boolean;
  originates_errors: boolean;
}

export interface AnalysisWindows {
  deployed_at: string;
  baseline_start: string;
  baseline_end: string;
  post_start: string;
  post_end: string;
  effective_post_end: string;
  as_of: string | null;
}

export interface FactorResult {
  factor: string;
  applies: boolean;
  points: number;
  reason: string;
}

export interface OriginCandidate {
  service: string;
  score: number;
  onset_at: string | null;
  factors: FactorResult[];
  reasoning: string[];
}

export interface TriageResult {
  candidates: OriginCandidate[];
  confidence: Confidence;
  probable_origin: string | null;
  shortlist: string[];
  top_score: number;
  margin: number | null;
  weights: Record<string, number>;
  summary: string;
}

export type Impact =
  | "AFFECTED_UPSTREAM"
  | "AFFECTED_DOWNSTREAM"
  | "HEALTHY_UPSTREAM"
  | "HEALTHY_DOWNSTREAM"
  | "UNKNOWN_UPSTREAM"
  | "UNKNOWN_DOWNSTREAM";

export interface RadiusNode {
  service: string;
  direction: "upstream" | "downstream";
  depth: number;
  impact: Impact;
  strict_status: ServiceStatus;
  relaxed_status: ServiceStatus | null;
  affected_via: "strict" | "relaxed" | null;
}

export interface BlastRadius {
  origin: string;
  nodes: RadiusNode[];
  affected: string[];
  not_in_radius: string[];
}

export interface GraphSnapshot {
  nodes: string[];
  edges: Array<{ source: string; target: string; call_count: number }>;
}

export interface RegressionAnalysisResult {
  engine_version: string;
  deployment: {
    service: string;
    environment?: string | null;
    version?: string | null;
    previous_version?: string | null;
    deployed_at: string;
  };
  windows: AnalysisWindows;
  config?: { name?: string; baseline_minutes?: number; warmup_minutes?: number; post_end_minutes?: number; min_requests?: number };
  status: ServiceStatus;
  severity: Severity | null;
  severity_service: string | null;
  services: ServiceEvidence[];
  relaxed_services: ServiceEvidence[];
  graph: GraphSnapshot;
  triage: TriageResult | null;
  blast_radius: BlastRadius | null;
  input?: {
    spans_received: number;
    other_environment_spans_dropped: number;
    duplicate_spans_dropped: number;
    spans_in_windows: number;
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Accepts `AnalysisOut.result` when it has the shape the UI reads (engine 1.x): windows,
 * services, graph. Anything else returns null and the page says the format is unknown instead
 * of rendering guesses.
 */
export function parseAnalysisResult(result: unknown): RegressionAnalysisResult | null {
  if (!isRecord(result)) return null;
  const { windows, services, graph, engine_version } = result;
  if (typeof engine_version !== "string" || !engine_version.startsWith("regression-engine/1.")) return null;
  if (!isRecord(windows) || typeof windows.deployed_at !== "string") return null;
  if (!Array.isArray(services) || !services.every((s) => isRecord(s) && typeof s.service === "string" && isRecord(s.baseline) && isRecord(s.post))) {
    return null;
  }
  if (!isRecord(graph) || !Array.isArray(graph.nodes) || !Array.isArray(graph.edges)) return null;
  return result as unknown as RegressionAnalysisResult;
}

/* ----------------------------------------------------------------------------------------- */
/* Risk assessments (M5): generated from the OpenAPI schema (docs/api/RISK_ASSESSMENTS.md).   */
/* ----------------------------------------------------------------------------------------- */

export type RiskBandName = "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";
export type RiskExplanation = S["Signal"];
export type RiskAssessment = S["RiskAssessmentOut"];
export type RiskAssessmentDetail = S["RiskAssessmentDetail"];
export type RiskAssessmentPage = S["RiskAssessmentPage"];
export type RiskModelStatus = S["ModelStatus"];

const RISK_BAND_NAMES: readonly string[] = ["LOW", "MEDIUM", "HIGH", "CRITICAL"];

/** The API types `risk_band` as a string; anything outside the four bands renders as unscored. */
export function riskBand(value: string | null | undefined): RiskBandName | null {
  return value && RISK_BAND_NAMES.includes(value) ? (value as RiskBandName) : null;
}

import type {
  AnalysisOut,
  AnalysisWindows,
  Impact,
  LatencyEvidence,
  OriginCandidate,
  RegressionAnalysisResult,
  ServiceEvidence,
  ServiceStatus,
} from "@/lib/api/types";
import {
  DASH,
  formatCount,
  formatMs,
  formatPp,
  formatRate,
  formatRelative,
  formatRpm,
  formatSignedMs,
  percentileLabel,
} from "@/lib/format";
import type { Tone } from "@/lib/tones";

/* ------------------------------------------------------------------------------------------ */
/* Which analysis to show                                                                     */
/* ------------------------------------------------------------------------------------------ */

const KIND_RANK: Record<AnalysisOut["kind"], number> = { early_5m: 0, early_10m: 1, final: 2 };

export const KIND_LABEL: Record<AnalysisOut["kind"], string> = {
  early_5m: "Early check (T+5 min)",
  early_10m: "Early check (T+10 min)",
  final: "Final",
};

/** The most complete analysis: final > early_10m > early_5m (the API's `latest` uses the same rule). */
export function pickLatest(analyses: AnalysisOut[]): AnalysisOut | null {
  return analyses.reduce<AnalysisOut | null>(
    (best, a) => (best === null || KIND_RANK[a.kind] > KIND_RANK[best.kind] ? a : best),
    null,
  );
}

/* ------------------------------------------------------------------------------------------ */
/* Status vocabulary                                                                          */
/* ------------------------------------------------------------------------------------------ */

export const STATUS_META: Record<ServiceStatus, { tone: Tone; label: string }> = {
  HEALTHY: { tone: "healthy", label: "Healthy" },
  WARNING: { tone: "warning", label: "Warning" },
  REGRESSED: { tone: "critical", label: "Regressed" },
  INSUFFICIENT_DATA: { tone: "unknown", label: "Insufficient data" },
};

export const CONFIDENCE_LABEL: Record<"HIGH" | "MEDIUM" | "LOW", string> = {
  HIGH: "High confidence",
  MEDIUM: "Medium confidence",
  LOW: "Low confidence",
};

/* ------------------------------------------------------------------------------------------ */
/* Before / after table                                                                       */
/* ------------------------------------------------------------------------------------------ */

export interface BeforeAfterRow {
  key: string;
  metric: string;
  before: string;
  after: string;
  delta: string;
  /** The uncertainty of the change, e.g. "95% CI +210 ms to +290 ms". */
  interval: string;
  /** Sample sizes, e.g. "n = 6,012 → 4,980 requests". */
  samples: string;
  /** Whether every gate for this metric passed (the metric counts as regressed). */
  regressed: boolean;
  /** Direction of the change, for an icon: "up", "down" or "flat". */
  direction: "up" | "down" | "flat";
  /** Throughput only: the drop gate passed (the service is WARNING, never REGRESSED for this alone). */
  warning?: boolean;
  note?: string;
}

function direction(delta: number | null | undefined): BeforeAfterRow["direction"] {
  if (delta === null || delta === undefined || delta === 0 || Number.isNaN(delta)) return "flat";
  return delta > 0 ? "up" : "down";
}

function samplesText(before: number | null | undefined, after: number | null | undefined, unit = "requests"): string {
  if ((before ?? null) === null && (after ?? null) === null) return DASH;
  return `n = ${formatCount(before)} → ${formatCount(after)} ${unit}`;
}

function latencyRow(ev: LatencyEvidence): BeforeAfterRow {
  const label = percentileLabel(ev.percentile);
  const confidence = Math.round(ev.ci_confidence * 100);
  const rel = ev.rel_delta === null ? "" : ` (${formatRelative(ev.rel_delta)})`;
  return {
    key: `latency-${label}`,
    metric: `${label} latency`,
    before: formatMs(ev.baseline_ms),
    after: formatMs(ev.post_ms),
    delta: `${formatSignedMs(ev.abs_delta_ms)}${rel}`,
    interval: `${confidence}% CI ${formatSignedMs(ev.ci_low_ms)} to ${formatSignedMs(ev.ci_high_ms)}`,
    samples: samplesText(ev.n_baseline, ev.n_post),
    regressed: ev.regressed,
    direction: direction(ev.abs_delta_ms),
    note: `Bootstrap of the ${label} difference, ${formatCount(ev.bootstrap_resamples)} resamples (seed ${ev.bootstrap_seed}).`,
  };
}

/**
 * Rows for one service's evidence: p50/p95/p99 latency, error rate and throughput, with deltas,
 * intervals and sample sizes exactly as the engine reported them. A percentile the engine did not
 * evaluate falls back to the window summary (no interval, marked "not tested").
 */
export function beforeAfterRows(ev: ServiceEvidence): BeforeAfterRow[] {
  const rows: BeforeAfterRow[] = [];
  const summaryKeys: Array<[number, keyof ServiceEvidence["baseline"]]> = [
    [0.5, "latency_p50_ms"],
    [0.95, "latency_p95_ms"],
    [0.99, "latency_p99_ms"],
  ];
  for (const [q, key] of summaryKeys) {
    const tested = ev.latency.find((l) => l.metric === "latency" && Math.abs(l.percentile - q) < 1e-9);
    if (tested) {
      rows.push(latencyRow(tested));
      continue;
    }
    const before = ev.baseline[key];
    const after = ev.post[key];
    const delta = before !== null && after !== null ? after - before : null;
    rows.push({
      key: `latency-${percentileLabel(q)}`,
      metric: `${percentileLabel(q)} latency`,
      before: formatMs(before),
      after: formatMs(after),
      delta: formatSignedMs(delta),
      interval: "Not tested",
      samples: samplesText(ev.baseline.requests, ev.post.requests),
      regressed: false,
      direction: direction(delta),
    });
  }

  const err = ev.error_rate;
  if (err) {
    rows.push({
      key: "error-rate",
      metric: "Error rate",
      before: formatRate(err.baseline_rate),
      after: formatRate(err.post_rate),
      delta: `${formatPp(err.abs_delta_pp)}${err.rel_delta === null ? "" : ` (${formatRelative(err.rel_delta)})`}`,
      interval: `one-sided p = ${formatP(err.p_value)}${err.exact_p_value === null || err.exact_p_value === undefined ? "" : `, exact p = ${formatP(err.exact_p_value)}`}`,
      samples: `${formatCount(err.baseline_errors)}/${formatCount(err.baseline_requests)} → ${formatCount(err.post_errors)}/${formatCount(err.post_requests)} errors`,
      regressed: err.regressed,
      direction: direction(err.abs_delta_pp),
      note: "Two-proportion z-test and Fisher exact test (one-sided: post > baseline).",
    });
  } else {
    const delta =
      ev.baseline.error_rate !== null && ev.post.error_rate !== null ? (ev.post.error_rate - ev.baseline.error_rate) * 100 : null;
    rows.push({
      key: "error-rate",
      metric: "Error rate",
      before: formatRate(ev.baseline.error_rate),
      after: formatRate(ev.post.error_rate),
      delta: formatPp(delta),
      interval: "Not tested",
      samples: `${formatCount(ev.baseline.errors)}/${formatCount(ev.baseline.requests)} → ${formatCount(ev.post.errors)}/${formatCount(ev.post.requests)} errors`,
      regressed: false,
      direction: direction(delta),
    });
  }

  const tp = ev.throughput;
  rows.push({
    key: "throughput",
    metric: "Throughput",
    before: formatRpm(tp?.baseline_rpm ?? ev.baseline.throughput_rpm),
    after: formatRpm(tp?.post_rpm ?? ev.post.throughput_rpm),
    delta: formatRelative(tp?.rel_change ?? null),
    interval: tp ? `drop gate ${tp.gate.passed ? "passed" : "not passed"} (≥ ${formatRelative(tp.gate.threshold, 0).replace("+", "")} drop)` : "Not tested",
    samples: samplesText(ev.baseline.requests, ev.post.requests),
    regressed: false,
    warning: tp?.dropped ?? false,
    direction: direction(tp?.rel_change ?? null),
    note: tp?.dropped ? "Throughput dropped: this makes a service WARNING, never REGRESSED on its own." : undefined,
  });
  return rows;
}

export function formatP(p: number | null | undefined): string {
  if (p === null || p === undefined || Number.isNaN(p)) return DASH;
  if (p === 0) return "< 1e-300";
  if (p < 0.001) return p.toExponential(1);
  return p.toFixed(3);
}

/* ------------------------------------------------------------------------------------------ */
/* Timeline                                                                                   */
/* ------------------------------------------------------------------------------------------ */

export interface TimelineModel {
  start: number;
  end: number;
  baseline: { from: number; to: number };
  deploy: number;
  /** Warm-up between the deploy and the start of the post window (excluded from the comparison). */
  warmup: { from: number; to: number };
  post: { from: number; to: number };
  /** Planned post window end when the analysis ran before it closed (early analyses). */
  plannedPostEnd: number;
  onset: { at: number; service: string; minutesAfterDeploy: number | null } | null;
}

function ms(iso: string): number {
  return new Date(iso).getTime();
}

/** Positions on the baseline | deploy | post timeline, from the analysis' resolved windows. */
export function timelineModel(windows: AnalysisWindows, result: RegressionAnalysisResult | null, origin: string | null): TimelineModel {
  const baselineFrom = ms(windows.baseline_start);
  const plannedPostEnd = ms(windows.post_end);
  const end = Math.max(plannedPostEnd, ms(windows.effective_post_end));
  let onset: TimelineModel["onset"] = null;
  if (result) {
    const ranked: Array<ServiceEvidence | undefined> = [];
    if (origin) ranked.push(result.services.find((s) => s.service === origin));
    ranked.push(result.services.find((s) => s.service === result.deployment.service));
    for (const ev of ranked) {
      if (ev?.onset?.onset_at) {
        onset = { at: ms(ev.onset.onset_at), service: ev.service, minutesAfterDeploy: ev.onset.minutes_after_deploy };
        break;
      }
    }
  }
  return {
    start: baselineFrom,
    end,
    baseline: { from: baselineFrom, to: ms(windows.baseline_end) },
    deploy: ms(windows.deployed_at),
    warmup: { from: ms(windows.deployed_at), to: ms(windows.post_start) },
    post: { from: ms(windows.post_start), to: ms(windows.effective_post_end) },
    plannedPostEnd,
    onset,
  };
}

/** Percentage position of `t` on the timeline. */
export function timelinePct(model: TimelineModel, t: number): number {
  const span = model.end - model.start;
  if (span <= 0) return 0;
  return Math.min(100, Math.max(0, ((t - model.start) / span) * 100));
}

/* ------------------------------------------------------------------------------------------ */
/* Probable origin                                                                            */
/* ------------------------------------------------------------------------------------------ */

export interface OriginView {
  /** null when confidence is LOW or the top candidates tie (ADR-0018): no origin is named. */
  service: string | null;
  confidence: "HIGH" | "MEDIUM" | "LOW";
  reasoning: string[];
  summary: string;
  candidate: OriginCandidate | null;
  runnersUp: OriginCandidate[];
  margin: number | null;
}

export function originView(result: RegressionAnalysisResult | null, analysis: AnalysisOut): OriginView | null {
  const triage = result?.triage ?? null;
  if (!triage) {
    if (!analysis.confidence) return null;
    return {
      service: analysis.probable_origin,
      confidence: analysis.confidence,
      reasoning: [],
      summary: "",
      candidate: null,
      runnersUp: [],
      margin: null,
    };
  }
  const named = triage.probable_origin;
  const candidate = (named ? triage.candidates.find((c) => c.service === named) : triage.candidates[0]) ?? null;
  return {
    service: named,
    confidence: triage.confidence,
    reasoning: candidate?.reasoning ?? [],
    summary: triage.summary,
    candidate,
    runnersUp: triage.candidates.filter((c) => c !== candidate).slice(0, 3),
    margin: triage.margin,
  };
}

/* ------------------------------------------------------------------------------------------ */
/* Blast radius                                                                               */
/* ------------------------------------------------------------------------------------------ */

export type RadiusRole = "origin" | "affected" | "healthy" | "unknown" | "outside";

export const RADIUS_ROLE_META: Record<RadiusRole, { label: string; tone: Tone; border: "solid" | "dashed" | "dotted" | "double" }> = {
  origin: { label: "Probable origin", tone: "critical", border: "double" },
  affected: { label: "Affected", tone: "high", border: "solid" },
  healthy: { label: "Healthy", tone: "healthy", border: "dashed" },
  unknown: { label: "Insufficient data", tone: "unknown", border: "dotted" },
  outside: { label: "Not in radius", tone: "unknown", border: "dotted" },
};

function roleOfImpact(impact: Impact): RadiusRole {
  if (impact.startsWith("AFFECTED")) return "affected";
  if (impact.startsWith("HEALTHY")) return "healthy";
  return "unknown";
}

export interface RadiusGraphNode {
  id: string;
  role: RadiusRole;
  direction: "origin" | "upstream" | "downstream" | "outside";
  depth: number;
  status: ServiceStatus | null;
  /** "strict" or "relaxed": which gate set marked it affected. */
  via: "strict" | "relaxed" | null;
}

export interface RadiusGraph {
  nodes: RadiusGraphNode[];
  edges: Array<{ id: string; source: string; target: string; callCount: number }>;
  /** True when the origin is the top candidate rather than a named probable origin (LOW confidence). */
  originIsCandidate: boolean;
}

export function radiusGraph(result: RegressionAnalysisResult | null): RadiusGraph | null {
  const radius = result?.blast_radius;
  if (!result || !radius) return null;
  const statusOf = new Map(result.services.map((s) => [s.service, s.status]));
  const nodes: RadiusGraphNode[] = [
    { id: radius.origin, role: "origin", direction: "origin", depth: 0, status: statusOf.get(radius.origin) ?? null, via: null },
  ];
  for (const n of radius.nodes) {
    nodes.push({
      id: n.service,
      role: roleOfImpact(n.impact),
      direction: n.direction,
      depth: n.depth,
      status: n.strict_status,
      via: n.affected_via,
    });
  }
  for (const service of radius.not_in_radius) {
    nodes.push({ id: service, role: "outside", direction: "outside", depth: 0, status: statusOf.get(service) ?? null, via: null });
  }
  const known = new Set(nodes.map((n) => n.id));
  const edges = result.graph.edges
    .filter((e) => known.has(e.source) && known.has(e.target))
    .map((e) => ({ id: `${e.source}->${e.target}`, source: e.source, target: e.target, callCount: e.call_count }));
  return { nodes, edges, originIsCandidate: result.triage?.probable_origin !== radius.origin };
}

/**
 * Column layout: upstream callers on the left (deepest furthest), the origin in the middle,
 * downstream dependencies on the right, services outside the radius in a last column.
 */
export function layoutRadius(graph: RadiusGraph, gapX = 210, gapY = 92): Map<string, { x: number; y: number }> {
  const columns = new Map<number, string[]>();
  const maxDown = Math.max(0, ...graph.nodes.filter((n) => n.direction === "downstream").map((n) => n.depth));
  for (const node of graph.nodes) {
    const col =
      node.direction === "origin"
        ? 0
        : node.direction === "upstream"
          ? -node.depth
          : node.direction === "downstream"
            ? node.depth
            : maxDown + 1;
    const list = columns.get(col) ?? [];
    list.push(node.id);
    columns.set(col, list);
  }
  const minCol = Math.min(...columns.keys());
  const positions = new Map<string, { x: number; y: number }>();
  for (const [col, ids] of columns) {
    ids.sort();
    const offset = ((ids.length - 1) * gapY) / 2;
    ids.forEach((id, i) => positions.set(id, { x: (col - minCol) * gapX, y: i * gapY - offset }));
  }
  return positions;
}

/* ------------------------------------------------------------------------------------------ */
/* Latency chart                                                                              */
/* ------------------------------------------------------------------------------------------ */

export interface LatencyPoint {
  t: number;
  p95: number | null;
  samples: number;
  exceeds: boolean;
}

export interface LatencySeries {
  service: string;
  points: LatencyPoint[];
  baselineP95: number;
  baselineCiHigh: number;
  ciConfidence: number;
  bucketSeconds: number;
  minSamples: number;
  onsetAt: number | null;
}

/**
 * The per-minute p95 the engine computed for onset detection (only for services whose p95
 * latency regression passed every gate). The engine stores no other latency time series, so
 * for every other service the chart is not drawn.
 */
export function latencySeries(result: RegressionAnalysisResult | null, preferred: Array<string | null | undefined>): LatencySeries | null {
  if (!result) return null;
  const order = [...preferred.filter((s): s is string => Boolean(s)), ...result.services.map((s) => s.service)];
  for (const name of order) {
    const ev = result.services.find((s) => s.service === name);
    const onset = ev?.onset;
    if (!ev || !onset || onset.buckets.length === 0) continue;
    return {
      service: ev.service,
      points: onset.buckets.map((b) => ({ t: ms(b.start), p95: b.p95_ms, samples: b.samples, exceeds: b.exceeds })),
      baselineP95: onset.baseline_p95_ms,
      baselineCiHigh: onset.baseline_p95_ci_high_ms,
      ciConfidence: onset.ci_confidence,
      bucketSeconds: onset.bucket_seconds,
      minSamples: onset.min_samples_per_bucket,
      onsetAt: onset.onset_at ? ms(onset.onset_at) : null,
    };
  }
  return null;
}

/** The evidence to show first: the probable origin, else the deployed service, else the worst. */
export function primaryEvidence(result: RegressionAnalysisResult | null, origin: string | null, deployed: string): ServiceEvidence | null {
  if (!result) return null;
  return (
    (origin ? result.services.find((s) => s.service === origin) : undefined) ??
    result.services.find((s) => s.service === deployed) ??
    result.services.find((s) => s.service === result.severity_service) ??
    result.services[0] ??
    null
  );
}

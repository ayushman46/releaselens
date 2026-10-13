import { describe, expect, it } from "vitest";

import {
  beforeAfterRows,
  latencySeries,
  layoutRadius,
  originView,
  pickLatest,
  primaryEvidence,
  radiusGraph,
  timelineModel,
  timelinePct,
} from "@/lib/analysis";
import { parseAnalysisResult, type AnalysisOut } from "@/lib/api/types";
import { analysisOut, analysisResult, paymentEvidence } from "@/test/analysis-fixture";

describe("beforeAfterRows", () => {
  const rows = beforeAfterRows(paymentEvidence);
  const byKey = Object.fromEntries(rows.map((r) => [r.key, r]));

  it("has p50/p95/p99 latency, error rate and throughput in that order", () => {
    expect(rows.map((r) => r.metric)).toEqual(["p50 latency", "p95 latency", "p99 latency", "Error rate", "Throughput"]);
  });

  it("formats latency with the delta, the bootstrap CI of the difference and the sample sizes", () => {
    expect(byKey["latency-p95"]).toMatchObject({
      before: "92.5 ms",
      after: "343 ms",
      delta: "+250 ms (+270.3%)",
      interval: "95% CI +210 ms to +290 ms",
      samples: "n = 6,012 → 4,980 requests",
      regressed: true,
      direction: "up",
    });
  });

  it("formats the error rate in percent and percentage points with its tests and counts", () => {
    expect(byKey["error-rate"]).toMatchObject({
      before: "0.20%",
      after: "3.69%",
      delta: "+3.49 pp (+1745.0%)",
      interval: "one-sided p = 1.0e-30, exact p = 2.0e-31",
      samples: "12/6,012 → 184/4,980 errors",
      regressed: true,
    });
  });

  it("reports throughput with the drop gate, never as regressed", () => {
    expect(byKey.throughput).toMatchObject({ before: "600 req/min", after: "553 req/min", delta: "−7.8%", regressed: false, warning: false, direction: "down" });
  });

  it("falls back to the window summary, marked 'Not tested', for a percentile the engine did not gate", () => {
    const ev = { ...paymentEvidence, latency: paymentEvidence.latency.filter((l) => l.percentile !== 0.99), error_rate: null };
    const fallback = beforeAfterRows(ev);
    expect(fallback.find((r) => r.key === "latency-p99")).toMatchObject({ before: "130 ms", after: "410 ms", delta: "+280 ms", interval: "Not tested", regressed: false });
    expect(fallback.find((r) => r.key === "error-rate")).toMatchObject({ interval: "Not tested", delta: "+3.49 pp" });
  });

  it("never invents a number for a missing value", () => {
    const empty = { ...paymentEvidence, latency: [], error_rate: null, throughput: null, baseline: { ...paymentEvidence.baseline, latency_p99_ms: null, throughput_rpm: null } };
    const p99 = beforeAfterRows(empty).find((r) => r.key === "latency-p99")!;
    expect(p99.before).toBe("—");
    expect(p99.delta).toBe("—");
  });
});

describe("timeline", () => {
  it("places baseline, deploy, post window and the onset from the resolved windows", () => {
    const model = timelineModel(analysisResult.windows, analysisResult, "payment");
    expect(timelinePct(model, model.baseline.from)).toBe(0);
    expect(timelinePct(model, model.end)).toBe(100);
    expect(Math.round(timelinePct(model, model.deploy))).toBe(56); // 10 of 18 minutes
    expect(model.onset).toMatchObject({ service: "payment", minutesAfterDeploy: 1 });
    expect(model.post.from).toBeGreaterThan(model.deploy);
  });
});

describe("probable origin", () => {
  it("names the origin with its confidence and reasoning", () => {
    const view = originView(analysisResult, analysisOut)!;
    expect(view.service).toBe("payment");
    expect(view.confidence).toBe("HIGH");
    expect(view.reasoning).toContain("payment originates the new errors");
    expect(view.runnersUp.map((c) => c.service)).toEqual(["orders"]);
  });

  it("names no origin when confidence is LOW, but keeps the top candidate", () => {
    const low = { ...analysisResult, triage: { ...analysisResult.triage!, confidence: "LOW" as const, probable_origin: null } };
    const view = originView(low, { ...analysisOut, probable_origin: null, confidence: "LOW" })!;
    expect(view.service).toBeNull();
    expect(view.candidate?.service).toBe("payment");
  });
});

describe("blast radius", () => {
  const graph = radiusGraph(analysisResult)!;

  it("classifies origin, affected, unknown and outside services", () => {
    const roles = Object.fromEntries(graph.nodes.map((n) => [n.id, n.role]));
    expect(roles).toEqual({ payment: "origin", orders: "affected", gateway: "affected", fraud: "unknown", inventory: "outside" });
    expect(graph.originIsCandidate).toBe(false);
    expect(graph.edges).toHaveLength(4);
  });

  it("lays callers left of the origin and dependencies right of it", () => {
    const pos = layoutRadius(graph);
    expect(pos.get("gateway")!.x).toBeLessThan(pos.get("orders")!.x);
    expect(pos.get("orders")!.x).toBeLessThan(pos.get("payment")!.x);
    expect(pos.get("payment")!.x).toBeLessThan(pos.get("fraud")!.x);
    expect(pos.get("inventory")!.x).toBeGreaterThan(pos.get("fraud")!.x);
  });
});

describe("latency series and evidence selection", () => {
  it("uses the onset buckets of the origin and keeps sparse buckets as gaps", () => {
    const series = latencySeries(analysisResult, ["payment"])!;
    expect(series.service).toBe("payment");
    expect(series.points.map((p) => p.p95)).toEqual([99, 340, null]);
    expect(series.baselineCiHigh).toBe(101);
  });

  it("returns null when no service has onset evidence (no invented chart)", () => {
    const none = { ...analysisResult, services: analysisResult.services.map((s) => ({ ...s, onset: null })) };
    expect(latencySeries(none, ["payment"])).toBeNull();
  });

  it("prefers the origin's evidence, then the deployed service", () => {
    expect(primaryEvidence(analysisResult, "orders", "payment")?.service).toBe("orders");
    expect(primaryEvidence(analysisResult, null, "payment")?.service).toBe("payment");
  });

  it("picks the most complete analysis kind", () => {
    const early = { ...analysisOut, kind: "early_5m" } as AnalysisOut;
    const mid = { ...analysisOut, kind: "early_10m" } as AnalysisOut;
    expect(pickLatest([early, analysisOut, mid])?.kind).toBe("final");
    expect(pickLatest([early, mid])?.kind).toBe("early_10m");
    expect(pickLatest([])).toBeNull();
  });
});

describe("parseAnalysisResult", () => {
  it("accepts engine 1.x results and rejects anything else", () => {
    expect(parseAnalysisResult(analysisResult)).not.toBeNull();
    expect(parseAnalysisResult({ ...analysisResult, engine_version: "regression-engine/2.0.0" })).toBeNull();
    expect(parseAnalysisResult({ ...analysisResult, services: [{ service: "x" }] })).toBeNull();
    expect(parseAnalysisResult(null)).toBeNull();
  });
});

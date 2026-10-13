import { describe, expect, it } from "vitest";

import { beforeAfterRows, latencySeries, originView, radiusGraph, timelineModel } from "@/lib/analysis";
import { parseAnalysisResult, type AnalysisOut } from "@/lib/api/types";
import engine from "@/test/fixtures/engine-payment-analysis.json";

/**
 * Contract check against real regression-engine output: the fixture is
 * `analyse_deployment(tracegen.payment_regression())` from packages/rl_core/tests (synthetic
 * traces, real engine), serialised with model_dump_json(). Regenerate it when the engine's
 * result format changes.
 */
describe("regression-engine result contract", () => {
  const result = parseAnalysisResult(engine);

  it("is accepted by the parser", () => {
    expect(result).not.toBeNull();
    expect(result!.engine_version).toMatch(/^regression-engine\/1\./);
  });

  it("drives every hero-page view without missing fields", () => {
    const r = result!;
    const analysis = { status: r.status, probable_origin: r.triage?.probable_origin ?? null, confidence: r.triage?.confidence ?? null } as AnalysisOut;
    const origin = originView(r, analysis)!;
    expect(origin.service).toBe("payment");
    expect(origin.reasoning.length).toBeGreaterThan(0);

    const payment = r.services.find((s) => s.service === "payment")!;
    const rows = beforeAfterRows(payment);
    expect(rows.map((row) => row.metric)).toEqual(["p50 latency", "p95 latency", "p99 latency", "Error rate", "Throughput"]);
    const p95 = rows.find((row) => row.key === "latency-p95")!;
    expect(p95.interval).toMatch(/^95% CI [+−]\d/);
    expect(p95.samples).toMatch(/^n = [\d,]+ → [\d,]+ requests$/);
    expect(rows.every((row) => !row.before.includes("NaN") && !row.after.includes("NaN"))).toBe(true);

    const graph = radiusGraph(r)!;
    expect(graph.nodes.find((n) => n.role === "origin")?.id).toBe("payment");
    expect(graph.nodes.some((n) => n.role === "affected")).toBe(true);

    const timeline = timelineModel(r.windows, r, "payment");
    expect(timeline.baseline.from).toBeLessThan(timeline.deploy);
    expect(timeline.deploy).toBeLessThanOrEqual(timeline.post.from);

    const series = latencySeries(r, ["payment"]);
    expect(series?.points.length).toBeGreaterThan(0);
  });
});

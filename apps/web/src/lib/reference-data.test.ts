import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import findings from "@/data/findings.json";
import primary from "@/data/ml/failed_or_errored.json";
import card from "@/data/ml/model-card.json";
import { buildReportView, headlineText } from "@/lib/findings";
import { buildModelView } from "@/lib/model-results";

const report = buildReportView(findings as unknown as Record<string, unknown>);
const model = buildModelView(primary as unknown as Record<string, unknown>);

describe("analytics findings (docs/analytics/findings.json)", () => {
  it("has the eight findings, each with charts, intervals and the correlation-not-causation caveat", () => {
    expect(report.findings.map((f) => f.key)).toEqual([
      "change_size",
      "src_test_churn",
      "previous_build",
      "duration",
      "projects",
      "monthly_trend",
      "core_team",
      "pull_requests",
    ]);
    for (const f of report.findings) {
      expect(f.caveats.some((c) => /not causation/i.test(c))).toBe(true);
      expect(f.charts.length).toBeGreaterThan(0);
      for (const chart of f.charts) {
        expect(chart.points.length).toBeGreaterThan(0);
        for (const p of chart.points) {
          if (p.value === null || p.n === 0) continue;
          expect(p.low).not.toBeNull();
          expect(p.high).not.toBeNull();
          expect(p.low!).toBeLessThanOrEqual(p.value);
          expect(p.high!).toBeGreaterThanOrEqual(p.value);
        }
      }
    }
  });

  it("formats the headline effect with its interval", () => {
    const prev = report.findings.find((f) => f.key === "previous_build")!;
    expect(headlineText(prev.headline)).toBe("Risk difference +58.86 pp (95% CI +58.35 pp to +59.37 pp), large");
    const size = report.findings.find((f) => f.key === "change_size")!;
    expect(headlineText(size.headline)).toBe("Cramér's V 0.042, negligible");
  });

  it("keeps the report-wide caveats", () => {
    expect(report.caveats[0]).toMatch(/^Association, not causation/);
    expect(report.population.labelled).toBe(128617);
  });
});

describe("model results (docs/ml/results/failed_or_errored.json)", () => {
  it("reads the champion, splits and test metrics", () => {
    expect(model.version).toBe("3");
    expect(model.promoted).toBe(true);
    expect(model.splits.map((s) => s.rows)).toEqual([90031, 19293, 19293]);
    expect(model.test.prAuc).toBeCloseTo(0.811, 3);
    expect(model.test.rocAuc).toBeCloseTo(0.881, 3);
    expect(model.test.confusion).toEqual({ tp: 3530, fp: 1065, fn: 1853, tn: 12845 });
    expect(model.calibration.reliability.length).toBe(10);
    expect(model.shap[0]?.feature).toBe("prev_build_failed");
    expect(model.bands.map((b) => b.scoreFrom)).toEqual([0, 64, 84, 94]);
  });

  it("quotes the model card's persistence caveat verbatim", () => {
    expect(card.version).toBe("release_risk_ci/3");
    expect(card.persistence).toContain("Much of the score reflects the project's recent build outcomes (a broken build tends to stay broken)");
  });
});

describe("src/data copies", () => {
  const docs = join(process.cwd(), "..", "..", "docs");
  it.skipIf(!existsSync(docs))("match docs/ (run `pnpm sync:data` after a new report or training run)", () => {
    expect(readFileSync(join(process.cwd(), "src/data/findings.json"), "utf8")).toBe(readFileSync(join(docs, "analytics/findings.json"), "utf8"));
    for (const file of ["failed_or_errored.json", "errored_excluded.json"]) {
      expect(readFileSync(join(process.cwd(), "src/data/ml", file), "utf8")).toBe(readFileSync(join(docs, "ml/results", file), "utf8"));
    }
  });
});

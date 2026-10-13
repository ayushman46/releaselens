/**
 * View model for docs/analytics/findings.json (report schema 1.x, docs/analytics/SCHEMA.md),
 * copied to src/data/findings.json by `pnpm sync:data`. Built on the server; only these trimmed
 * shapes reach the browser.
 */

export interface RatePoint {
  key: string;
  label: string;
  /** Proportion in [0, 1], or seconds for duration charts; null when undefined (n = 0). */
  value: number | null;
  low: number | null;
  high: number | null;
  n: number;
  /** Shown but flagged (e.g. a low-volume month). */
  flagged?: boolean;
}

export interface ChartSpec {
  id: string;
  title: string;
  unit: "rate" | "seconds";
  /** "vertical" bars for few ordered categories, "horizontal" for long labels, "line" for months. */
  kind: "vertical" | "horizontal" | "line";
  intervalLabel: string;
  points: RatePoint[];
  note?: string;
}

export interface FindingView {
  key: string;
  title: string;
  question: string;
  summary: string;
  caveats: string[];
  populationN: number;
  headline: {
    effectName: string;
    value: number | null;
    low: number | null;
    high: number | null;
    magnitude: string | null;
    test: string;
    pHolm: number | null;
  };
  charts: ChartSpec[];
}

export interface ReportView {
  runId: string;
  schemaVersion: string;
  dataset: { source: string; subset: string; first: string; last: string };
  population: { labelled: number; projects: number; rate: number; low: number; high: number; labelRule: string };
  caveats: string[];
  findings: FindingView[];
}

type Json = Record<string, unknown>;
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const str = (v: unknown, fallback = ""): string => (typeof v === "string" ? v : fallback);
const arr = (v: unknown): Json[] => (Array.isArray(v) ? (v as Json[]) : []);

function ratePoint(item: Json, label?: string, flagged?: boolean): RatePoint {
  return {
    key: str(item.key),
    label: label ?? str(item.label, str(item.key)),
    value: num(item.rate),
    low: num(item.ci95_low),
    high: num(item.ci95_high),
    n: num(item.n) ?? 0,
    flagged,
  };
}

const WILSON = "95% Wilson interval";

const PREV_LABEL: Record<string, string> = {
  "leakage_safe|prev_failed": "Previous finished build failed",
  "leakage_safe|prev_passed": "Previous finished build passed",
  "naive|prev_failed": "Previous started build failed (naive)",
  "naive|prev_passed": "Previous started build passed (naive)",
};

function cellLabel(key: string): string {
  const [a, b] = key.split("|");
  const first = a === "pull_request" ? "PR" : a === "push" ? "Push" : a;
  const second = b === "core_team" ? "core team" : b === "non_core" ? "non-core" : b;
  return `${first}, ${second}`;
}

function chartsFor(finding: Json): ChartSpec[] {
  const s = (finding.series ?? {}) as Json;
  const key = str(finding.key);
  switch (key) {
    case "change_size":
      return [{ id: "buckets", title: "Failure rate by lines changed (source + test)", unit: "rate", kind: "vertical", intervalLabel: WILSON, points: arr(s.buckets).map((b) => ratePoint(b)) }];
    case "src_test_churn":
      return [
        {
          id: "cells",
          title: "By source-churn quartile, with and without test changes",
          unit: "rate",
          kind: "horizontal",
          intervalLabel: WILSON,
          points: arr(s.cells).map((c) => ratePoint(c, `${str(c.quartile)} (${str(c.quartile_label)}), ${c.tests_changed ? "tests changed" : "no test change"}`)),
        },
        { id: "ratio", title: "By test share of changed lines", unit: "rate", kind: "vertical", intervalLabel: WILSON, points: arr(s.test_change_ratio).map((c) => ratePoint(c)) },
      ];
    case "previous_build":
      return [{ id: "bars", title: "Failure rate given the previous build's outcome", unit: "rate", kind: "horizontal", intervalLabel: WILSON, points: arr(s.bars).map((b) => ratePoint(b, PREV_LABEL[str(b.key)] ?? str(b.key))) }];
    case "duration":
      return [
        {
          id: "by_status",
          title: "Summed job time by outcome (median, interquartile range)",
          unit: "seconds",
          kind: "vertical",
          intervalLabel: "Interquartile range (25th–75th percentile)",
          points: arr(s.by_status).map((b) => ({ key: str(b.key), label: str(b.key), value: num(b.median_s), low: num(b.q25_s), high: num(b.q75_s), n: num(b.n) ?? 0 })),
        },
      ];
    case "projects":
      return [
        { id: "top", title: "Highest failure rates (projects with ≥ 200 builds)", unit: "rate", kind: "horizontal", intervalLabel: WILSON, points: arr(s.top).map((p) => ratePoint(p, str(p.key))) },
        { id: "bottom", title: "Lowest failure rates", unit: "rate", kind: "horizontal", intervalLabel: WILSON, points: arr(s.bottom).map((p) => ratePoint(p, str(p.key))) },
      ];
    case "monthly_trend":
      return [
        {
          id: "months",
          title: "Monthly failure rate",
          unit: "rate",
          kind: "line",
          intervalLabel: WILSON,
          points: arr(s.months).map((m) => ratePoint(m, str(m.key), m.low_volume === true)),
          note: "Hollow markers are low-volume months (fewer than 100 labelled builds): plotted, excluded from the test.",
        },
      ];
    case "core_team":
      return [
        { id: "groups", title: "Core team vs non-core contributors", unit: "rate", kind: "vertical", intervalLabel: WILSON, points: arr(s.groups).map((g) => ratePoint(g)) },
        { id: "cells", title: "Split by build type", unit: "rate", kind: "horizontal", intervalLabel: WILSON, points: arr(s.by_pull_request).map((g) => ratePoint(g, cellLabel(str(g.key)))) },
      ];
    case "pull_requests":
      return [
        { id: "groups", title: "Pull-request vs push builds", unit: "rate", kind: "vertical", intervalLabel: WILSON, points: arr(s.groups).map((g) => ratePoint(g)) },
        { id: "cells", title: "Split by contributor", unit: "rate", kind: "horizontal", intervalLabel: WILSON, points: arr(s.by_core_team).map((g) => ratePoint(g, cellLabel(str(g.key)))) },
      ];
    default:
      return [];
  }
}

export function buildReportView(report: Json): ReportView {
  const dataset = (report.dataset ?? {}) as Json;
  const population = (report.population ?? {}) as Json;
  const failure = (population.failure ?? {}) as Json;
  return {
    runId: str(report.run_id),
    schemaVersion: str(report.schema_version),
    dataset: { source: str(dataset.source), subset: str(dataset.subset), first: str(dataset.first_started_at), last: str(dataset.last_started_at) },
    population: {
      labelled: num(population.labelled_builds) ?? 0,
      projects: num(population.projects) ?? 0,
      rate: num(failure.rate) ?? 0,
      low: num(failure.ci95_low) ?? 0,
      high: num(failure.ci95_high) ?? 0,
      labelRule: str(population.label_rule),
    },
    caveats: (Array.isArray(report.caveats) ? report.caveats : []).map((c) => str(c)),
    findings: arr(report.findings).map((f) => {
      const h = (f.headline ?? {}) as Json;
      const pop = (f.population ?? {}) as Json;
      return {
        key: str(f.key),
        title: str(f.title),
        question: str(f.question),
        summary: str(f.summary),
        caveats: (Array.isArray(f.caveats) ? f.caveats : []).map((c) => str(c)),
        populationN: num(pop.n) ?? 0,
        headline: {
          effectName: str(h.effect_name),
          value: num(h.effect_value),
          low: num(h.effect_ci95_low),
          high: num(h.effect_ci95_high),
          magnitude: typeof h.effect_magnitude === "string" ? h.effect_magnitude : null,
          test: str(h.test),
          pHolm: num(h.p_value_holm),
        },
        charts: chartsFor(f),
      };
    }),
  };
}

/** "Risk difference +8.22 pp (95% CI +5.34 to +11.20 pp)", "Cramér's V 0.042 (negligible)". */
export function headlineText(h: FindingView["headline"]): string {
  const fmt = (v: number | null) => {
    if (v === null) return "—";
    if (h.effectName === "risk_difference") return `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v * 100).toFixed(2)} pp`;
    return v.toFixed(3);
  };
  const name =
    h.effectName === "risk_difference" ? "Risk difference" : h.effectName === "cramers_v" ? "Cramér's V" : h.effectName === "cliffs_delta" ? "Cliff's delta" : h.effectName;
  const ci = h.low !== null && h.high !== null ? ` (95% CI ${fmt(h.low)} to ${fmt(h.high)})` : "";
  const mag = h.magnitude ? `, ${h.magnitude}` : "";
  return `${name} ${fmt(h.value)}${ci}${mag}`;
}

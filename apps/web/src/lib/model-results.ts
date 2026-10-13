/**
 * View model for docs/ml/results/*.json (schema rl_ml.results/1), copied to src/data/ml by
 * `pnpm sync:data`. Built on the server; missing fields stay null and render as "—".
 */

type Json = Record<string, unknown>;
const obj = (v: unknown): Json => (v && typeof v === "object" && !Array.isArray(v) ? (v as Json) : {});
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const str = (v: unknown): string | null => (typeof v === "string" ? v : null);

export interface SplitView {
  name: string;
  rows: number | null;
  start: string | null;
  end: string | null;
  baseRate: number | null;
  projects: number | null;
}

export interface ModelView {
  variant: string;
  labelDefinition: string | null;
  modelName: string | null;
  version: string | null;
  runId: string | null;
  promoted: boolean;
  family: string | null;
  featureCount: number;
  generatedAt: string | null;
  gitSha: string | null;
  datasetSource: string | null;
  splits: SplitView[];
  test: {
    n: number | null;
    baseRate: number | null;
    prAuc: number | null;
    prAucCi: [number, number] | null;
    rocAuc: number | null;
    rocAucCi: [number, number] | null;
    precisionTop10: number | null;
    liftTop10: number | null;
    threshold: number | null;
    precision: number | null;
    recall: number | null;
    f1: number | null;
    confusion: { tp: number; fp: number; fn: number; tn: number } | null;
  };
  baselines: Array<{ name: string; prAuc: number | null; rocAuc: number | null }>;
  calibration: {
    method: string | null;
    passed: boolean | null;
    ece: number | null;
    brier: number | null;
    brierSkill: number | null;
    slope: number | null;
    probabilityExposed: boolean;
    reliability: Array<{ predicted: number; observed: number; n: number }>;
  };
  shap: Array<{ feature: string; label: string; meanAbs: number }>;
  shapMethod: string | null;
  bands: Array<{ band: string; scoreFrom: number | null; share: number | null; n: number | null; failureRate: number | null; precisionAtOrAbove: number | null; recallAtOrAbove: number | null }>;
  byPrevious: Array<{ key: string; label: string; n: number | null; baseRate: number | null; prAuc: number | null; rocAuc: number | null }>;
  coldStart: { prAuc: number | null; rocAuc: number | null } | null;
  heldOut: { prAuc: number | null; rocAuc: number | null; projects: number | null } | null;
}

const CANDIDATE_LABEL: Record<string, string> = {
  majority_class: "Majority class",
  heuristic_rule: "Heuristic rule (previous build failed or churn > p90)",
  logistic_regression: "Logistic regression",
  random_forest: "Random forest",
  xgboost: "XGBoost",
  xgboost_time: "XGBoost + time features",
};

export function buildModelView(results: Json): ModelView {
  const registry = obj(results.registry);
  const promotion = obj(registry.promotion);
  const selection = obj(results.selection);
  const classBalance = obj(results.class_balance);
  const splits = obj(obj(results.splits).chronological);
  const test = obj(results.test);
  const final = obj(test.final);
  const atT = obj(final.at_threshold);
  const cm = obj(atT.confusion_matrix);
  const ci = (o: unknown): [number, number] | null => {
    const v = obj(o);
    const low = num(v.low);
    const high = num(v.high);
    return low !== null && high !== null ? [low, high] : null;
  };
  const calibration = obj(test.calibration);
  const gate = obj(calibration.gate);
  const bands = obj(results.bands);
  const scoreCut = obj(bands.risk_score);
  const testBands = obj(test.bands);
  const exclusive = obj(testBands.exclusive);
  const atOrAbove = obj(testBands.at_or_above);
  const byPrev = obj(test.by_previous_outcome);
  const cross = obj(obj(results.cross_project).results);
  const unseen = obj(cross.unseen_projects_test_window);
  const cold = obj(test.cold_start);

  return {
    variant: str(results.variant) ?? "unknown",
    labelDefinition: str(results.label_definition),
    modelName: str(registry.registered_model) ?? str(promotion.model),
    version: str(registry.version) ?? str(promotion.version),
    runId: str(registry.run_id),
    promoted: promotion.promoted === true,
    family: str(selection.display) ?? str(selection.final_model),
    featureCount: Array.isArray(selection.features) ? selection.features.length : 0,
    generatedAt: str(results.generated_at),
    gitSha: str(obj(results.git).sha),
    datasetSource: str(obj(results.data).source),
    splits: (["train", "validation", "test"] as const).map((name) => {
      const s = obj(splits[name]);
      const cb = obj(classBalance[name]);
      return { name, rows: num(s.rows), start: str(s.start), end: str(s.end), baseRate: num(cb.base_rate), projects: num(cb.projects) };
    }),
    test: {
      n: num(final.n),
      baseRate: num(final.base_rate),
      prAuc: num(final.pr_auc),
      prAucCi: ci(final.pr_auc_ci95_project_bootstrap),
      rocAuc: num(final.roc_auc),
      rocAucCi: ci(final.roc_auc_ci95_project_bootstrap),
      precisionTop10: num(final.precision_at_top_10pct),
      liftTop10: num(final.lift_at_top_10pct),
      threshold: num(atT.threshold),
      precision: num(atT.precision),
      recall: num(atT.recall),
      f1: num(atT.f1),
      confusion:
        num(cm.tp) !== null && num(cm.fp) !== null && num(cm.fn) !== null && num(cm.tn) !== null
          ? { tp: num(cm.tp)!, fp: num(cm.fp)!, fn: num(cm.fn)!, tn: num(cm.tn)! }
          : null,
    },
    baselines: Object.entries(obj(test.reference_candidates)).map(([name, v]) => ({
      name: CANDIDATE_LABEL[name] ?? name,
      prAuc: num(obj(v).pr_auc),
      rocAuc: num(obj(v).roc_auc),
    })),
    calibration: {
      method: str(calibration.selected_method),
      passed: typeof gate.passed === "boolean" ? gate.passed : null,
      ece: num(gate.ece_10_equal_mass),
      brier: num(gate.brier),
      brierSkill: num(gate.brier_skill_score),
      slope: num(gate.calibration_slope),
      probabilityExposed: test.probability_exposed === true,
      reliability: (Array.isArray(gate.reliability) ? gate.reliability : [])
        .map((r) => ({ predicted: num(obj(r).p_mean), observed: num(obj(r).observed_rate), n: num(obj(r).n) ?? 0 }))
        .filter((r): r is { predicted: number; observed: number; n: number } => r.predicted !== null && r.observed !== null),
    },
    shap: (Array.isArray(obj(results.explainability).global) ? (obj(results.explainability).global as unknown[]) : [])
      .map((g) => ({ feature: str(obj(g).feature) ?? "", label: str(obj(g).label) ?? str(obj(g).feature) ?? "", meanAbs: num(obj(g).mean_abs_shap) ?? 0 }))
      .filter((g) => g.feature),
    shapMethod: str(obj(results.explainability).global_method),
    bands: ["LOW", "MEDIUM", "HIGH", "CRITICAL"].map((band) => {
      const e = obj(exclusive[band]);
      const a = obj(atOrAbove[band]);
      return {
        band,
        scoreFrom: band === "LOW" ? 0 : num(scoreCut[band]),
        share: num(e.share),
        n: num(e.n),
        failureRate: num(e.failure_rate),
        precisionAtOrAbove: num(a.precision),
        recallAtOrAbove: num(a.recall),
      };
    }),
    byPrevious: [
      ["previous_passed", "After a passing build (new breakage)"],
      ["previous_failed", "After a failing build (persistence)"],
      ["no_finished_history", "No finished history"],
    ].map(([key, label]) => {
      const v = obj(byPrev[key!]);
      return { key: key!, label: label!, n: num(v.n), baseRate: num(v.base_rate), prAuc: num(v.pr_auc), rocAuc: num(v.roc_auc) };
    }),
    coldStart: Object.keys(cold).length ? { prAuc: num(cold.pr_auc), rocAuc: num(cold.roc_auc) } : null,
    heldOut: Object.keys(unseen).length ? { prAuc: num(unseen.pr_auc), rocAuc: num(unseen.roc_auc), projects: num(unseen.projects) } : null,
  };
}

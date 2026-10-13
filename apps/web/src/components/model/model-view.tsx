import { ExternalLink, Quote } from "lucide-react";
import type { ReactNode } from "react";

import { ReliabilityLoader } from "@/components/model/reliability-loader";
import { ServingStatus } from "@/components/model/serving-status";
import { StatusPill } from "@/components/rl/status-pill";
import { Callout } from "@/components/ui/callout";
import { formatCount, formatDate, formatNumber } from "@/lib/format";
import type { ModelView } from "@/lib/model-results";

export const MODEL_CARD_URL = "https://github.com/ayushman46/releaselens/blob/main/docs/ml/MODEL_CARD.md";

export interface ModelCardExcerpts {
  version: string | null;
  persistence: string | null;
  ci_failure_not_outage: string | null;
  travis_transfer: string | null;
  explanations: string | null;
  intended_use: string[];
}

const pct = (v: number | null, digits = 1) => (v === null ? "—" : `${(v * 100).toFixed(digits)}%`);

function Panel({ title, children, id }: { title: string; children: ReactNode; id: string }) {
  return (
    <section aria-labelledby={id} className="rounded-xl border bg-panel shadow-panel">
      <h2 id={id} className="border-b px-5 py-3.5 text-sm font-semibold">
        {title}
      </h2>
      <div className="px-5 py-4">{children}</div>
    </section>
  );
}

function Metric({ label, value, detail }: { label: string; value: string; detail?: string }) {
  return (
    <div className="rounded-xl border bg-panel p-4 shadow-panel">
      <p className="text-xs font-medium text-muted-foreground">{label}</p>
      <p className="mt-2 text-2xl font-semibold tracking-tight tabular-nums">{value}</p>
      {detail ? <p className="mt-1 text-xs text-muted-foreground">{detail}</p> : null}
    </div>
  );
}

function ConfusionMatrix({ cm }: { cm: NonNullable<ModelView["test"]["confusion"]> }) {
  const total = cm.tp + cm.fp + cm.fn + cm.tn;
  const cell = (n: number, label: string, emphasis: boolean) => (
    <td className={`border px-3 py-3 text-center tabular-nums ${emphasis ? "bg-subtle font-semibold text-foreground" : "text-foreground-secondary"}`}>
      {formatCount(n)}
      <span className="block text-[11px] font-normal text-muted-foreground">
        {label} · {pct(n / total)}
      </span>
    </td>
  );
  return (
    <table className="w-full max-w-md border-collapse text-sm">
      <caption className="mb-2 text-left text-xs text-muted-foreground">Test set at the F1-optimal threshold chosen on validation</caption>
      <thead>
        <tr>
          <td />
          <th scope="col" className="px-3 py-1.5 text-xs font-medium text-muted-foreground">Predicted fail</th>
          <th scope="col" className="px-3 py-1.5 text-xs font-medium text-muted-foreground">Predicted pass</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <th scope="row" className="pr-3 text-right text-xs font-medium text-muted-foreground">Failed</th>
          {cell(cm.tp, "true positive", true)}
          {cell(cm.fn, "false negative", false)}
        </tr>
        <tr>
          <th scope="row" className="pr-3 text-right text-xs font-medium text-muted-foreground">Passed</th>
          {cell(cm.fp, "false positive", false)}
          {cell(cm.tn, "true negative", true)}
        </tr>
      </tbody>
    </table>
  );
}

export function ModelOverview({ model, sensitivity, card }: { model: ModelView; sensitivity: ModelView | null; card: ModelCardExcerpts }) {
  const maxShap = Math.max(1e-9, ...model.shap.map((s) => s.meanAbs));
  const t = model.test;
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <StatusPill tone={model.promoted ? "healthy" : "unknown"} label={model.promoted ? "Champion" : "Not promoted"} />
        <span className="font-mono text-foreground">{card.version ?? (model.modelName && model.version ? `${model.modelName}/${model.version}` : "—")}</span>
        <span className="text-muted-foreground">
          · {model.family} over {model.featureCount} pre-build features · trained {formatDate(model.generatedAt)}
        </span>
        <a href={MODEL_CARD_URL} target="_blank" rel="noreferrer noopener" className="ml-auto inline-flex items-center gap-1 font-medium text-brand-text hover:underline">
          Model card
          <ExternalLink aria-hidden="true" className="size-3.5" />
          <span className="sr-only">(opens in a new tab)</span>
        </a>
      </div>

      <ServingStatus />

      {card.persistence ? (
        <figure className="rounded-xl border border-warning-border bg-warning-bg px-5 py-4">
          <figcaption className="flex items-center gap-2 text-sm font-semibold text-foreground">
            <Quote aria-hidden="true" className="size-4 text-warning" />
            Most of the signal is build-failure persistence
          </figcaption>
          <blockquote className="mt-2 text-sm leading-6 text-foreground-secondary">“{card.persistence}”</blockquote>
          <p className="mt-2 text-xs text-muted-foreground">Model card, “Persistence vs new breakage”</p>
        </figure>
      ) : null}

      <section aria-label="Test-set metrics" className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Metric
          label="PR-AUC (test)"
          value={formatNumber(t.prAuc)}
          detail={`${t.prAucCi ? `95% project-bootstrap CI ${t.prAucCi[0].toFixed(3)}–${t.prAucCi[1].toFixed(3)}; ` : ""}base rate ${formatNumber(t.baseRate)}`}
        />
        <Metric label="ROC-AUC (test)" value={formatNumber(t.rocAuc)} detail={t.rocAucCi ? `95% CI ${t.rocAucCi[0].toFixed(3)}–${t.rocAucCi[1].toFixed(3)}` : undefined} />
        <Metric label="Precision / recall" value={`${pct(t.precision)} / ${pct(t.recall)}`} detail={`F1 ${formatNumber(t.f1)} at threshold ${formatNumber(t.threshold)}`} />
        <Metric label="Precision, top 10% of scores" value={pct(t.precisionTop10)} detail={t.liftTop10 !== null ? `${t.liftTop10.toFixed(2)}× the base rate` : undefined} />
      </section>

      <div className="grid gap-6 xl:grid-cols-2">
        <Panel title="Data and splits" id="splits-title">
          <table className="w-full text-sm">
            <caption className="sr-only">Chronological split sizes and date ranges</caption>
            <thead>
              <tr className="text-xs text-muted-foreground">
                <th scope="col" className="py-1 text-left font-medium">Split</th>
                <th scope="col" className="py-1 text-right font-medium">Builds</th>
                <th scope="col" className="py-1 text-right font-medium">Projects</th>
                <th scope="col" className="py-1 text-right font-medium">Failure rate</th>
                <th scope="col" className="py-1 pl-3 text-left font-medium">Dates</th>
              </tr>
            </thead>
            <tbody className="tabular-nums">
              {model.splits.map((s) => (
                <tr key={s.name} className="border-t">
                  <th scope="row" className="py-1.5 text-left font-medium capitalize">{s.name}</th>
                  <td className="py-1.5 text-right">{formatCount(s.rows)}</td>
                  <td className="py-1.5 text-right">{formatCount(s.projects)}</td>
                  <td className="py-1.5 text-right">{pct(s.baseRate)}</td>
                  <td className="py-1.5 pl-3 text-xs text-muted-foreground">
                    {formatDate(s.start)} – {formatDate(s.end)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-3 text-xs leading-5 text-muted-foreground">
            {model.datasetSource} · {model.labelDefinition}. Chronological split; the test set was evaluated once, after selection was locked.
          </p>
        </Panel>

        <Panel title="Confusion matrix" id="cm-title">
          {t.confusion ? <ConfusionMatrix cm={t.confusion} /> : <p className="text-sm text-muted-foreground">Not reported.</p>}
        </Panel>

        <Panel title="Against baselines (test PR-AUC)" id="baselines-title">
          <table className="w-full text-sm">
            <caption className="sr-only">Test PR-AUC and ROC-AUC of the final model and every reference candidate</caption>
            <thead>
              <tr className="text-xs text-muted-foreground">
                <th scope="col" className="py-1 text-left font-medium">Model</th>
                <th scope="col" className="py-1 text-right font-medium">PR-AUC</th>
                <th scope="col" className="py-1 text-right font-medium">ROC-AUC</th>
              </tr>
            </thead>
            <tbody className="tabular-nums">
              {model.baselines.map((b) => (
                <tr key={b.name} className="border-t">
                  <th scope="row" className="py-1.5 text-left font-normal">{b.name}</th>
                  <td className="py-1.5 text-right">{formatNumber(b.prAuc)}</td>
                  <td className="py-1.5 text-right">{formatNumber(b.rocAuc)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h3 className="mt-4 text-xs font-medium tracking-wide text-muted-foreground uppercase">By the previous build&apos;s outcome</h3>
          <table className="mt-1 w-full text-sm">
            <caption className="sr-only">Test metrics split by the previous build outcome</caption>
            <thead>
              <tr className="text-xs text-muted-foreground">
                <th scope="col" className="py-1 text-left font-medium">Subset</th>
                <th scope="col" className="py-1 text-right font-medium">n</th>
                <th scope="col" className="py-1 text-right font-medium">Base rate</th>
                <th scope="col" className="py-1 text-right font-medium">PR-AUC</th>
                <th scope="col" className="py-1 text-right font-medium">ROC-AUC</th>
              </tr>
            </thead>
            <tbody className="tabular-nums">
              {model.byPrevious.map((b) => (
                <tr key={b.key} className="border-t">
                  <th scope="row" className="py-1.5 text-left font-normal">{b.label}</th>
                  <td className="py-1.5 text-right">{formatCount(b.n)}</td>
                  <td className="py-1.5 text-right">{pct(b.baseRate)}</td>
                  <td className="py-1.5 text-right">{formatNumber(b.prAuc)}</td>
                  <td className="py-1.5 text-right">{formatNumber(b.rocAuc)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-3 text-xs leading-5 text-muted-foreground">
            {model.coldStart ? `Cold start (history masked): PR-AUC ${formatNumber(model.coldStart.prAuc)}, ROC-AUC ${formatNumber(model.coldStart.rocAuc)}. ` : ""}
            {model.heldOut ? `Unseen projects: PR-AUC ${formatNumber(model.heldOut.prAuc)} over ${formatCount(model.heldOut.projects)} projects. ` : ""}
            {sensitivity ? `Errored builds excluded (sensitivity run): PR-AUC ${formatNumber(sensitivity.test.prAuc)} at base rate ${formatNumber(sensitivity.test.baseRate)}.` : ""}
          </p>
        </Panel>

        <Panel title="Calibration" id="calibration-title">
          <div className="mb-3 flex flex-wrap items-center gap-2 text-xs text-foreground-secondary">
            <StatusPill
              tone={model.calibration.passed ? "healthy" : "warning"}
              label={model.calibration.passed ? "Calibration gate passed" : "Calibration gate not passed"}
              size="sm"
            />
            <span>
              {model.calibration.method} · ECE {formatNumber(model.calibration.ece, 4)} · Brier {formatNumber(model.calibration.brier)} · Brier skill{" "}
              {formatNumber(model.calibration.brierSkill)} · slope {formatNumber(model.calibration.slope)}
            </span>
          </div>
          <ReliabilityLoader points={model.calibration.reliability} />
          <p className="mt-2 text-xs text-muted-foreground">
            {model.calibration.probabilityExposed
              ? "Because the gate passed, the API returns a calibrated probability next to the 0–100 score."
              : "The gate did not pass, so the API returns only the 0–100 score."}
          </p>
        </Panel>

        <Panel title="Risk bands (test set)" id="bands-title">
          <table className="w-full text-sm">
            <caption className="sr-only">Score cut-offs, share of builds and failure rate per band</caption>
            <thead>
              <tr className="text-xs text-muted-foreground">
                <th scope="col" className="py-1 text-left font-medium">Band</th>
                <th scope="col" className="py-1 text-right font-medium">Score from</th>
                <th scope="col" className="py-1 text-right font-medium">Share</th>
                <th scope="col" className="py-1 text-right font-medium">Failure rate</th>
                <th scope="col" className="py-1 text-right font-medium">Precision ≥ band</th>
                <th scope="col" className="py-1 text-right font-medium">Recall ≥ band</th>
              </tr>
            </thead>
            <tbody className="tabular-nums">
              {model.bands.map((b) => (
                <tr key={b.band} className="border-t">
                  <th scope="row" className="py-1.5 text-left font-medium">{b.band.charAt(0) + b.band.slice(1).toLowerCase()}</th>
                  <td className="py-1.5 text-right">{b.scoreFrom ?? "—"}</td>
                  <td className="py-1.5 text-right">
                    {pct(b.share)} <span className="text-xs text-muted-foreground">({formatCount(b.n)})</span>
                  </td>
                  <td className="py-1.5 text-right">{pct(b.failureRate)}</td>
                  <td className="py-1.5 text-right">{b.band === "LOW" ? "—" : pct(b.precisionAtOrAbove)}</td>
                  <td className="py-1.5 text-right">{b.band === "LOW" ? "—" : pct(b.recallAtOrAbove)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-3 text-xs text-muted-foreground">Cut-offs were set on validation at recall 0.80 / 0.50 / 0.20 for Medium / High / Critical.</p>
        </Panel>

        <Panel title="Global feature importance (mean |SHAP|)" id="shap-title">
          <ol className="space-y-2">
            {model.shap.slice(0, 15).map((s) => (
              <li key={s.feature} className="text-sm">
                <div className="flex items-baseline justify-between gap-3">
                  <span className="truncate text-foreground">{s.label}</span>
                  <span className="shrink-0 text-xs text-muted-foreground tabular-nums">{s.meanAbs.toFixed(3)}</span>
                </div>
                <div aria-hidden="true" className="mt-1 h-1.5 rounded-full bg-subtle">
                  <div className="h-full rounded-full bg-brand" style={{ width: `${(s.meanAbs / maxShap) * 100}%` }} />
                </div>
              </li>
            ))}
          </ol>
          <p className="mt-3 text-xs text-muted-foreground">
            {model.shapMethod}. {card.explanations ? `Explanations ${card.explanations}` : ""}
          </p>
        </Panel>
      </div>

      <Callout tone="warning" title="Limits to keep in mind">
        <ul className="list-disc space-y-1 pl-4">
          {card.ci_failure_not_outage ? <li>{card.ci_failure_not_outage}</li> : null}
          {card.travis_transfer ? <li>{card.travis_transfer}</li> : null}
          {card.intended_use.slice(1).map((u) => (
            <li key={u}>{u}</li>
          ))}
        </ul>
      </Callout>
      <p className="text-xs text-muted-foreground">
        Source: docs/ml/results/{model.variant}.json (MLflow run <span className="font-mono">{model.runId ?? "—"}</span>, git{" "}
        <span className="font-mono">{model.gitSha?.slice(0, 7) ?? "—"}</span>).
      </p>
    </div>
  );
}

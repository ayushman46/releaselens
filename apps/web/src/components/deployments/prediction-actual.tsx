"use client";

import { ArrowRight } from "lucide-react";

import { AnalysisStatusPill } from "@/components/rl/analysis-status";
import { RiskBadge } from "@/components/rl/risk-badge";
import { SeverityBadge } from "@/components/rl/severity-badge";
import { useRiskAssessment } from "@/lib/api/queries";
import type { AnalysisOut, DeploymentDetail } from "@/lib/api/types";
import { KIND_LABEL } from "@/lib/analysis";

/**
 * What was expected before the deploy next to what happened after it. They answer different
 * questions (CI failure risk of the change vs. a runtime regression), so they are shown side by
 * side, never scored against each other.
 */
export function PredictionActual({
  deployment,
  analysis,
  orgId,
}: {
  deployment: DeploymentDetail;
  analysis: AnalysisOut | null;
  orgId: string | undefined;
}) {
  const risk = useRiskAssessment(orgId, deployment.risk_assessment_id);
  return (
    <section aria-labelledby="pva-title" className="rounded-xl border bg-panel p-4 shadow-panel">
      <h2 id="pva-title" className="sr-only">
        Prediction versus actual
      </h2>
      <div className="grid items-center gap-3 sm:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)]">
        <div>
          <p className="text-xs font-medium tracking-wide text-muted-foreground uppercase">Predicted before deploy</p>
          <div className="mt-1.5 flex flex-wrap items-center gap-2 text-sm">
            {deployment.risk_assessment_id ? (
              risk.data ? (
                <RiskBadge score={risk.data.risk_score} band={risk.data.risk_band} size="sm" />
              ) : (
                <span className="text-muted-foreground">{risk.isPending ? "Loading…" : "Assessment linked, not readable yet"}</span>
              )
            ) : (
              <RiskBadge score={null} band={null} size="sm" />
            )}
            <span className="text-xs text-muted-foreground">CI failure risk of the change</span>
          </div>
        </div>
        <ArrowRight aria-hidden="true" className="hidden size-4 text-muted-foreground sm:block" />
        <div>
          <p className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
            Observed after deploy{analysis ? ` · ${KIND_LABEL[analysis.kind]}` : ""}
          </p>
          <div className="mt-1.5 flex flex-wrap items-center gap-2 text-sm">
            {analysis ? (
              <>
                <AnalysisStatusPill status={analysis.status} size="sm" />
                {analysis.status !== "INSUFFICIENT_DATA" && analysis.status !== "HEALTHY" ? (
                  <SeverityBadge severity={analysis.severity_band} score={analysis.severity_score} size="sm" />
                ) : null}
              </>
            ) : (
              <AnalysisStatusPill status={null} size="sm" />
            )}
            <span className="text-xs text-muted-foreground">runtime regression check</span>
          </div>
        </div>
      </div>
      <p className="mt-3 text-xs leading-5 text-muted-foreground">
        The risk score predicts CI build failure, not production regressions. Read the two together; a low risk score
        never rules out a regression.
      </p>
    </section>
  );
}

"use client";

import { Gauge } from "lucide-react";
import Link from "next/link";

import { RiskBadge } from "@/components/rl/risk-badge";
import { StatusPill } from "@/components/rl/status-pill";
import { CiRunSignal } from "@/components/risk/ci-run-signal";
import { ShapBars } from "@/components/risk/shap-bars";
import { LoadingState } from "@/components/ui/skeleton";
import { describeApiError } from "@/lib/api/errors";
import { isEndpointMissing, useRiskAssessment } from "@/lib/api/queries";
import type { DeploymentDetail, RiskAssessment } from "@/lib/api/types";

/**
 * Pre-deployment CI failure risk of the change behind this deployment, when the pipeline linked
 * an assessment (`risk_assessment_id`). The CI run is a separate signal: it is shown next to the
 * score, never folded into it (it would leak the answer).
 */
export function RiskCard({ deployment, orgId }: { deployment: DeploymentDetail; orgId: string | undefined }) {
  const id = deployment.risk_assessment_id;
  const query = useRiskAssessment(orgId, id);

  return (
    <section aria-labelledby="risk-card-title" className="flex flex-col rounded-xl border bg-panel shadow-panel">
      <div className="flex flex-wrap items-center gap-2 border-b px-5 py-3.5">
        <Gauge aria-hidden="true" className="size-4 text-muted-foreground" />
        <h2 id="risk-card-title" className="text-sm font-semibold">
          Pre-deployment risk
        </h2>
        {query.data ? <RiskBadge score={query.data.risk_score} band={query.data.risk_band} size="sm" className="ml-auto" /> : null}
      </div>
      <div className="flex-1 space-y-4 px-5 py-4 text-sm">
        {!id ? (
          <p className="text-muted-foreground">
            No risk assessment was linked to this deployment. Send <code className="font-mono text-xs">risk_assessment_id</code>{" "}
            with the deployment event to see the change&apos;s CI failure risk here.
          </p>
        ) : query.isPending ? (
          <LoadingState label="Loading the risk assessment…" lines={3} />
        ) : query.isError ? (
          <p className="text-muted-foreground">
            {isEndpointMissing(query.error)
              ? "Linked to assessment "
              : `Assessment `}
            <code className="font-mono text-xs break-all">{id}</code>
            {isEndpointMissing(query.error)
              ? ". Reading assessments arrives with M5 (risk API); the link is kept and will show here then."
              : ` could not be loaded: ${describeApiError(query.error)}`}
          </p>
        ) : (
          <RiskSummary assessment={query.data} />
        )}

        <div className="border-t pt-3">
          <h3 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">CI result (separate signal)</h3>
          <CiRunSignal orgId={orgId} repository={deployment.repository} commitSha={deployment.commit_sha} ciRunUrl={deployment.ci_run_url} />
        </div>
      </div>
    </section>
  );
}

function RiskSummary({ assessment }: { assessment: RiskAssessment }) {
  return (
    <>
      <p className="text-foreground-secondary">
        CI failure risk {assessment.risk_score}/100 ({assessment.risk_band.toLowerCase()} band) from{" "}
        <code className="font-mono text-xs">{assessment.model_version}</code>. A score of historical CI failure risk, not the chance of an
        outage.
        {assessment.history.cold_start ? " Cold start: no build history was available, so the score is less reliable." : ""}
      </p>
      <ShapBars increasing={assessment.top_increasing} reducing={assessment.top_reducing} limit={3} />
      <div className="flex flex-wrap items-center gap-2">
        {assessment.probability !== null ? (
          <StatusPill tone="info" label={`Calibrated probability ${(assessment.probability * 100).toFixed(0)}%`} size="sm" />
        ) : null}
        <Link href={`/app/risk/${assessment.assessment_id}`} className="text-xs font-medium text-brand-text hover:underline">
          Full assessment
        </Link>
      </div>
    </>
  );
}

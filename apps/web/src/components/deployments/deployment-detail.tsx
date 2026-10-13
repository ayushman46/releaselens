"use client";

import { ExternalLink, Info } from "lucide-react";
import dynamic from "next/dynamic";
import { useMemo, useState } from "react";

import { AnalysisTimeline } from "@/components/deployments/analysis-timeline";
import { BeforeAfterTable } from "@/components/deployments/before-after-table";
import { VersionChange } from "@/components/deployments/deployments-view";
import { OriginCard } from "@/components/deployments/origin-card";
import { PredictionActual } from "@/components/deployments/prediction-actual";
import { RiskCard } from "@/components/deployments/risk-card";
import { AnalysisStatusPill } from "@/components/rl/analysis-status";
import { ApiErrorState } from "@/components/rl/api-error-state";
import { SeverityBadge } from "@/components/rl/severity-badge";
import { useSession } from "@/components/session/session-provider";
import { Callout } from "@/components/ui/callout";
import { KeyValueList } from "@/components/ui/key-value";
import { PageHeader } from "@/components/ui/page-header";
import { LoadingState, Skeleton } from "@/components/ui/skeleton";
import {
  KIND_LABEL,
  latencySeries,
  originView,
  pickLatest,
  primaryEvidence,
  radiusGraph,
  timelineModel,
} from "@/lib/analysis";
import { useDeployment, useDeploymentAnalysis } from "@/lib/api/queries";
import { parseAnalysisResult, type AnalysisOut, type AnalysisWindows, type DeploymentDetail } from "@/lib/api/types";
import { DASH, formatCount, formatDateTime, formatRelativeTime, shortSha } from "@/lib/format";

// React Flow measures the DOM; render it on the client only.
const BlastRadius = dynamic(() => import("@/components/graph/blast-radius").then((m) => m.BlastRadius), {
  ssr: false,
  loading: () => <Skeleton className="h-72 rounded-xl" />,
});
const LatencyChart = dynamic(() => import("@/components/deployments/latency-chart").then((m) => m.LatencyChart), {
  ssr: false,
  loading: () => <Skeleton className="h-72 rounded-xl" />,
});

function isWindows(value: unknown): value is AnalysisWindows {
  if (!value || typeof value !== "object") return false;
  const w = value as Record<string, unknown>;
  return ["deployed_at", "baseline_start", "baseline_end", "post_start", "post_end", "effective_post_end"].every((k) => typeof w[k] === "string");
}

function DeploymentFacts({ deployment }: { deployment: DeploymentDetail }) {
  const labels = Object.entries(deployment.labels ?? {});
  return (
    <div className="rounded-xl border bg-panel px-5 shadow-panel">
      <KeyValueList
        labelWidth="sm:grid-cols-[minmax(0,8rem)_minmax(0,1fr)]"
        items={[
          { label: "Version", value: <VersionChange previous={deployment.previous_version} next={deployment.version} /> },
          { label: "Environment", value: deployment.environment },
          {
            label: "Deployed",
            value: (
              <time dateTime={deployment.deployed_at}>
                {formatDateTime(deployment.deployed_at)} ({formatRelativeTime(deployment.deployed_at)})
              </time>
            ),
          },
          {
            label: "Commit",
            value: (
              <span className="font-mono text-xs" title={deployment.commit_sha}>
                {shortSha(deployment.commit_sha)}
                {deployment.repository ? <span className="text-muted-foreground"> · {deployment.repository}</span> : null}
                {deployment.pull_request ? <span className="text-muted-foreground"> · PR #{deployment.pull_request}</span> : null}
              </span>
            ),
          },
          { label: "Deployer", value: deployment.deployer ?? DASH },
          ...(deployment.ci_run_url
            ? [
                {
                  label: "CI run",
                  value: (
                    <a href={deployment.ci_run_url} target="_blank" rel="noreferrer noopener" className="inline-flex items-center gap-1 text-brand-text hover:underline">
                      Open
                      <ExternalLink aria-hidden="true" className="size-3.5" />
                      <span className="sr-only">(opens in a new tab)</span>
                    </a>
                  ),
                },
              ]
            : []),
          ...(labels.length ? [{ label: "Labels", value: labels.map(([k, v]) => `${k}=${String(v)}`).join(", ") }] : []),
          {
            label: "Analysis jobs",
            value: deployment.analysis_jobs.length
              ? deployment.analysis_jobs.map((j) => `${KIND_LABEL[j.kind as AnalysisOut["kind"]] ?? j.kind}: ${j.status.toLowerCase()}`).join(" · ")
              : "None scheduled",
          },
        ]}
      />
    </div>
  );
}

export function AnalysisSection({ analysis, deployment }: { analysis: AnalysisOut; deployment: DeploymentDetail }) {
  const result = useMemo(() => parseAnalysisResult(analysis.result), [analysis.result]);
  const origin = useMemo(() => originView(result, analysis), [result, analysis]);
  const originService = origin?.service ?? origin?.candidate?.service ?? null;
  const windows = result?.windows ?? (isWindows(analysis.windows) ? analysis.windows : null);
  const timeline = windows ? timelineModel(windows, result, originService) : null;
  const evidence = primaryEvidence(result, originService, deployment.service);
  const [selected, setSelected] = useState<string | null>(null);
  const shown = (selected && result?.services.find((s) => s.service === selected)) || evidence;
  const series = latencySeries(result, [originService, deployment.service]);
  const graph = radiusGraph(result);

  return (
    <div className="space-y-6">
      {!result ? (
        <Callout tone="warning" title="Unknown result format">
          This analysis was produced by {analysis.engine_version}, whose result this page cannot read. The verdict above is from the API&apos;s
          summary fields.
        </Callout>
      ) : null}
      {timeline ? <AnalysisTimeline model={timeline} /> : null}
      <OriginCard origin={origin} />
      <BlastRadius graph={graph} />
      {result && shown ? (
        <BeforeAfterTable services={result.services} selected={shown} onSelect={setSelected} origin={originService} />
      ) : null}
      <LatencyChart series={series} />
      {result?.input ? (
        <p className="flex items-start gap-2 text-xs leading-5 text-muted-foreground">
          <Info aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
          Based on {formatCount(result.input.spans_in_windows)} spans in the windows ({formatCount(result.input.spans_received)} loaded,{" "}
          {formatCount(result.input.duplicate_spans_dropped)} duplicates and {formatCount(result.input.other_environment_spans_dropped)} from other
          environments dropped). Engine {analysis.engine_version}, analysed {formatDateTime(analysis.updated_at)}.
        </p>
      ) : null}
    </div>
  );
}

export function DeploymentDetailView({ deploymentId }: { deploymentId: string }) {
  const { orgId } = useSession();
  const deployment = useDeployment(orgId, deploymentId);
  const analysis = useDeploymentAnalysis(orgId, deploymentId);
  const [kind, setKind] = useState<AnalysisOut["kind"] | null>(null);

  const analyses = analysis.data?.analyses ?? [];
  const latest = analysis.data?.latest ?? pickLatest(analyses);
  const current = (kind && analyses.find((a) => a.kind === kind)) || latest;

  if (deployment.isPending) return <LoadingState label="Loading the deployment…" lines={6} />;
  if (deployment.isError) {
    return <ApiErrorState what="This deployment" error={deployment.error} onRetry={() => void deployment.refetch()} />;
  }
  const d = deployment.data;

  return (
    <>
      <PageHeader
        title={`${d.service} ${d.version}`}
        description={
          <>
            {d.previous_version ? `${d.previous_version} → ${d.version}` : `First recorded version ${d.version}`} in {d.environment}, deployed{" "}
            {formatDateTime(d.deployed_at)}
            {d.deployer ? ` by ${d.deployer}` : ""}, commit <span className="font-mono">{shortSha(d.commit_sha)}</span>.
          </>
        }
        breadcrumbs={[
          { label: "Dashboard", href: "/app" },
          { label: "Deployments", href: "/app/deployments" },
          { label: `${d.service} ${d.version}` },
        ]}
        meta={
          current ? (
            <>
              <AnalysisStatusPill status={current.status} />
              {current.status === "REGRESSED" || current.status === "WARNING" ? (
                <SeverityBadge severity={current.severity_band} score={current.severity_score} />
              ) : null}
            </>
          ) : null
        }
      />

      <div className="space-y-6">
        <PredictionActual deployment={d} analysis={current ?? null} orgId={orgId} />
        <div className="grid gap-6 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
          <DeploymentFacts deployment={d} />
          <RiskCard deployment={d} orgId={orgId} />
        </div>

        <section aria-labelledby="analysis-title" className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h2 id="analysis-title" className="text-base font-semibold tracking-tight">
              Regression analysis
            </h2>
            {analyses.length > 1 ? (
              <div role="group" aria-label="Analysis checkpoint" className="inline-flex rounded-md border bg-panel p-0.5">
                {[...analyses]
                  .sort((a, b) => a.created_at.localeCompare(b.created_at))
                  .map((a) => (
                    <button
                      key={a.kind}
                      type="button"
                      aria-pressed={a.kind === current?.kind}
                      onClick={() => setKind(a.kind)}
                      className="h-7 rounded px-2.5 text-xs font-medium text-muted-foreground hover:text-foreground aria-pressed:bg-subtle aria-pressed:text-foreground"
                    >
                      {KIND_LABEL[a.kind]}
                    </button>
                  ))}
              </div>
            ) : null}
          </div>
          {analysis.isPending ? (
            <LoadingState label="Loading the analysis…" lines={5} />
          ) : analysis.isError ? (
            <ApiErrorState what="The analysis" error={analysis.error} onRetry={() => void analysis.refetch()} />
          ) : !current ? (
            <Callout tone="info" title="No analysis yet">
              The first check runs about 5 minutes after the deploy and the final one when the comparison window closes (15 minutes by
              default). This page refreshes every minute until the final analysis exists.
            </Callout>
          ) : (
            <AnalysisSection key={current.id} analysis={current} deployment={d} />
          )}
        </section>
      </div>
    </>
  );
}

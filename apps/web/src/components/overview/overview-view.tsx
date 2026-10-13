"use client";

import { Activity, ArrowRight, Boxes, Gauge, GitPullRequest, Radio, Rocket } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";

import { VersionChange } from "@/components/deployments/deployments-view";
import { AnalysisStatusPill } from "@/components/rl/analysis-status";
import { ApiErrorState } from "@/components/rl/api-error-state";
import { SeverityBadge } from "@/components/rl/severity-badge";
import { StatCard } from "@/components/rl/stat-card";
import { approxP95 } from "@/components/services/services-view";
import { useSession } from "@/components/session/session-provider";
import { SetupSteps } from "@/components/shell/setup-steps";
import { LoadingState } from "@/components/ui/skeleton";
import { isEndpointMissing, useDeployments, useRegressions, useRiskAssessments, useServices } from "@/lib/api/queries";
import { formatRate, formatRelativeTime } from "@/lib/format";

const WEEK_MS = 7 * 24 * 3600 * 1000;

/** Count of items in the last 7 days from a newest-first first page; "50+" when the page is full. */
export function countThisWeek(items: Array<{ at: string }>, hasMore: boolean, now = Date.now()): string {
  const recent = items.filter((i) => now - new Date(i.at).getTime() <= WEEK_MS).length;
  return hasMore && recent === items.length ? `${recent}+` : String(recent);
}

function Panel({ title, href, linkLabel, children }: { title: string; href: string; linkLabel: string; children: ReactNode }) {
  const id = `panel-${title.toLowerCase().replace(/\W+/g, "-")}`;
  return (
    <section aria-labelledby={id} className="flex flex-col rounded-xl border bg-panel shadow-panel">
      <div className="flex items-center justify-between gap-3 border-b px-5 py-3">
        <h2 id={id} className="text-sm font-semibold">
          {title}
        </h2>
        <Link href={href} className="inline-flex items-center gap-1 text-xs font-medium text-brand-text hover:underline">
          {linkLabel}
          <ArrowRight aria-hidden="true" className="size-3" />
        </Link>
      </div>
      <div className="flex-1">{children}</div>
    </section>
  );
}

function Empty({ children }: { children: ReactNode }) {
  return <p className="px-5 py-6 text-sm text-muted-foreground">{children}</p>;
}

export function OverviewView() {
  const { orgId } = useSession();
  const deployments = useDeployments(orgId, {});
  const regressions = useRegressions(orgId, {});
  const services = useServices(orgId);
  const risk = useRiskAssessments(orgId);

  const depItems = deployments.data?.pages[0]?.items ?? [];
  const regItems = regressions.data?.pages[0]?.items ?? [];
  const svcItems = services.data?.pages.flatMap((p) => p.items) ?? [];
  const riskItems = risk.data?.pages[0]?.items ?? [];
  const riskMissing = risk.isError && isEndpointMissing(risk.error);
  const reporting = svcItems.filter((s) => s.health.requests > 0).length;

  const steps = [
    {
      title: "Score pull requests",
      description: "Add the GitHub Action. It computes change facts in your runner and requests a CI failure risk score.",
      href: "/app/risk",
      cta: "Set up",
      icon: GitPullRequest,
      milestone: riskMissing ? "M5" : undefined,
      connected: riskItems.length > 0,
    },
    {
      title: "Report deployments",
      description: "Post one event per release from your CD pipeline. This starts the before/after comparison.",
      href: "/app/deployments",
      cta: "Set up",
      icon: Rocket,
      connected: depItems.length > 0,
    },
    {
      title: "Send traces",
      description: "Point your OpenTelemetry Collector at the OTLP endpoint. Services and the service map come from these traces.",
      href: "/app/services",
      cta: "Set up",
      icon: Radio,
      connected: svcItems.length > 0,
    },
  ];
  const allConnected = steps.every((s) => s.connected);

  return (
    <>
      <section aria-label="Last 7 days" className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label="Deployments · 7 days"
          icon={Rocket}
          value={deployments.data ? countThisWeek(depItems.map((d) => ({ at: d.deployed_at })), Boolean(deployments.data.pages[0]?.next_cursor)) : null}
          missingReason={
            deployments.isError
              ? "Deployments could not be loaded."
              : "No deployment events yet. The count starts with the first POST /api/v1/deployments from your pipeline."
          }
        />
        <StatCard
          label="Regressions · 7 days"
          icon={Activity}
          value={regressions.data ? countThisWeek(regItems.map((r) => ({ at: r.deployed_at })), Boolean(regressions.data.pages[0]?.next_cursor)) : null}
          missingReason={
            regressions.isError
              ? "Regressions could not be loaded (see the panel below)."
              : "Regressions are found by comparing traces before and after each deployment."
          }
        />
        <StatCard
          label="Risk assessments · 7 days"
          icon={Gauge}
          value={risk.data ? countThisWeek(riskItems.map((r) => ({ at: r.created_at })), Boolean(risk.data.pages[0]?.next_cursor)) : null}
          missingReason={
            riskMissing
              ? "The risk assessment read API arrives with M5; this count appears then."
              : "Risk scores appear when the ReleaseLens GitHub Action runs on your pull requests."
          }
        />
        <StatCard
          label="Services reporting · 15 min"
          icon={Boxes}
          value={services.data && svcItems.length > 0 ? `${reporting} of ${svcItems.length}` : null}
          missingReason={services.isError ? "The service catalog could not be loaded." : "Services appear when spans arrive at the OTLP endpoint."}
        />
      </section>

      {!allConnected ? (
        <div className="mt-6">
          <SetupSteps title="Connect ReleaseLens" steps={steps} />
        </div>
      ) : null}

      <div className="mt-6 grid gap-6 xl:grid-cols-2">
        <Panel title="Active regressions" href="/app/regressions" linkLabel="All regressions">
          {regressions.isPending ? (
            <LoadingState className="p-5" label="Loading regressions…" />
          ) : regressions.isError ? (
            <ApiErrorState what="Regressions" error={regressions.error} className="m-4 py-6" onRetry={() => void regressions.refetch()} />
          ) : regItems.length === 0 ? (
            <Empty>No deployment has a Regressed or Warning verdict. Each deployment is checked after release.</Empty>
          ) : (
            <ul className="divide-y">
              {regItems.slice(0, 5).map((r) => (
                <li key={r.analysis_id}>
                  <Link href={`/app/deployments/${r.deployment_id}`} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-5 py-3 hover:bg-subtle/60">
                    <span className="font-medium text-foreground">{r.service}</span>
                    <VersionChange previous={r.previous_version} next={r.version} />
                    <span className="ml-auto flex items-center gap-2">
                      <AnalysisStatusPill status={r.status} size="sm" />
                      <SeverityBadge severity={r.severity_band} score={r.severity_score} size="sm" />
                    </span>
                    <span className="w-full text-xs text-muted-foreground">
                      {r.probable_origin ? `Probable origin: ${r.probable_origin}` : "No probable origin named"} · {r.environment} ·{" "}
                      {formatRelativeTime(r.deployed_at)}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <Panel title="Recent deployments" href="/app/deployments" linkLabel="All deployments">
          {deployments.isPending ? (
            <LoadingState className="p-5" label="Loading deployments…" />
          ) : deployments.isError ? (
            <ApiErrorState what="Deployments" error={deployments.error} className="m-4 py-6" onRetry={() => void deployments.refetch()} />
          ) : depItems.length === 0 ? (
            <Empty>No deployments yet. Report one from your pipeline to start the before/after checks.</Empty>
          ) : (
            <ul className="divide-y">
              {depItems.slice(0, 6).map((d) => (
                <li key={d.id}>
                  <Link href={`/app/deployments/${d.id}`} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-5 py-3 hover:bg-subtle/60">
                    <span className="font-medium text-foreground">{d.service}</span>
                    <VersionChange previous={d.previous_version} next={d.version} />
                    <span className="ml-auto text-xs text-muted-foreground">
                      {d.environment} · {formatRelativeTime(d.deployed_at)}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <Panel title="Risk assessments this week" href="/app/risk" linkLabel="Risk analysis">
          {risk.isPending ? (
            <LoadingState className="p-5" label="Loading risk assessments…" />
          ) : riskMissing ? (
            <Empty>Risk assessments arrive with M5. Scores will be listed here, newest first, once the risk API serves them.</Empty>
          ) : risk.isError ? (
            <ApiErrorState what="Risk assessments" error={risk.error} className="m-4 py-6" />
          ) : riskItems.length === 0 ? (
            <Empty>No pull requests scored yet. Add the GitHub Action to score them before merge.</Empty>
          ) : (
            <ul className="divide-y">
              {riskItems.slice(0, 5).map((a) => (
                <li key={a.assessment_id}>
                  <Link href={`/app/risk/${a.assessment_id}`} className="flex items-center gap-3 px-5 py-3 hover:bg-subtle/60">
                    <span className="truncate font-medium text-foreground">
                      {a.repository}
                      {a.pull_request ? ` #${a.pull_request}` : ""}
                    </span>
                    <span className="ml-auto text-xs text-muted-foreground tabular-nums">
                      {a.risk_score}/100 · {a.risk_band.toLowerCase()}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <Panel title="Service health · last 15 min" href="/app/services" linkLabel="All services">
          {services.isPending ? (
            <LoadingState className="p-5" label="Loading services…" />
          ) : services.isError ? (
            <ApiErrorState what="Service health" error={services.error} className="m-4 py-6" onRetry={() => void services.refetch()} />
          ) : svcItems.length === 0 ? (
            <Empty>No services reporting. Send OpenTelemetry traces to see traffic, errors and latency per service.</Empty>
          ) : (
            <table className="w-full text-sm">
              <caption className="sr-only">Service health over the last 15 complete minutes, highest error rate first</caption>
              <thead>
                <tr className="border-b text-xs text-muted-foreground">
                  <th scope="col" className="px-5 py-2 text-left font-medium">Service</th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">Req/min</th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">Error rate</th>
                  <th scope="col" className="px-5 py-2 text-right font-medium">p95</th>
                </tr>
              </thead>
              <tbody className="tabular-nums">
                {[...svcItems]
                  .sort((a, b) => (b.health.error_rate ?? -1) - (a.health.error_rate ?? -1))
                  .slice(0, 6)
                  .map((s) => (
                    <tr key={`${s.service}|${s.environment ?? ""}`} className="border-b last:border-b-0">
                      <th scope="row" className="px-5 py-2 text-left font-medium">
                        {s.service}
                        {s.environment ? <span className="ml-1.5 text-xs font-normal text-muted-foreground">{s.environment}</span> : null}
                      </th>
                      <td className="px-3 py-2 text-right text-foreground-secondary">{s.health.request_rate_per_min.toFixed(1)}</td>
                      <td className="px-3 py-2 text-right text-foreground-secondary">{formatRate(s.health.error_rate)}</td>
                      <td className="px-5 py-2 text-right text-foreground-secondary">{approxP95(s.health)}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          )}
        </Panel>
      </div>
    </>
  );
}

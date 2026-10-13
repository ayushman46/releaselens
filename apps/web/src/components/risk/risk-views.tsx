"use client";

import { ExternalLink } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo } from "react";

import { CiRunSignal } from "@/components/risk/ci-run-signal";
import { LoadMore } from "@/components/rl/analysis-status";
import { ApiErrorState } from "@/components/rl/api-error-state";
import { ListFilters, useUrlFilters } from "@/components/rl/list-filters";
import { RiskBadge } from "@/components/rl/risk-badge";
import { RiskSetup } from "@/components/rl/setup-empty-states";
import { ShapBars } from "@/components/risk/shap-bars";
import { useSession } from "@/components/session/session-provider";
import { Callout } from "@/components/ui/callout";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { KeyValueList } from "@/components/ui/key-value";
import { PageHeader } from "@/components/ui/page-header";
import { LoadingState } from "@/components/ui/skeleton";
import { isEndpointMissing, useRiskAssessment, useRiskAssessments } from "@/lib/api/queries";
import type { RiskAssessment, RiskAssessmentDetail } from "@/lib/api/types";
import { DASH, formatDateTime, formatRelativeTime, shortSha } from "@/lib/format";
import { storageKey } from "@/lib/saved-views";

const BAND_RANK = { LOW: 0, MEDIUM: 1, HIGH: 2, CRITICAL: 3 } as const;

const COLUMNS: DataTableColumn<RiskAssessment>[] = [
  {
    id: "change",
    header: "Change",
    rowHeader: true,
    sortValue: (a) => a.repository,
    cell: (a) => (
      <Link href={`/app/risk/${a.assessment_id}`} tabIndex={-1} className="rounded-sm hover:underline">
        {a.repository}
        {a.pull_request ? ` #${a.pull_request}` : ""}
        <span className="block font-mono text-[11px] font-normal text-muted-foreground">
          {shortSha(a.commit_sha)} · {a.event === "pull_request" ? "pull request" : a.event}
        </span>
      </Link>
    ),
  },
  {
    id: "score",
    header: "CI failure risk",
    sortValue: (a) => BAND_RANK[a.risk_band] * 1000 + a.risk_score,
    cell: (a) => <RiskBadge score={a.risk_score} band={a.risk_band} size="sm" />,
  },
  {
    id: "top",
    header: "Top factor",
    cell: (a) => (a.top_increasing[0] ? a.top_increasing[0].label : <span className="text-muted-foreground">{DASH}</span>),
  },
  { id: "model", header: "Model", cell: (a) => <code className="font-mono text-xs">{a.model_version}</code> },
  {
    id: "when",
    header: "Scored",
    sortValue: (a) => new Date(a.created_at),
    cell: (a) => (
      <time dateTime={a.created_at} title={formatDateTime(a.created_at)}>
        {formatRelativeTime(a.created_at)}
      </time>
    ),
  },
];

export function RiskMeaning() {
  return (
    <Callout tone="info" title="What the score means" className="mb-6">
      It ranks how likely a change is to fail CI, learned from historical builds. It is not the chance of an outage, and it is
      shown as a 0–100 score. Explanations describe the model, not the cause of a failure.{" "}
      <Link href="/app/model" className="font-medium text-foreground underline underline-offset-2">
        See the model and its limits
      </Link>
    </Callout>
  );
}

export function RiskListView() {
  const { orgId } = useSession();
  const router = useRouter();
  const [filters, setFilters] = useUrlFilters(["repository", "risk_band"]);
  const query = useRiskAssessments(orgId, filters);
  const rows = useMemo(() => query.data?.pages.flatMap((p) => p.items) ?? [], [query.data]);
  const filtered = Object.values(filters).some(Boolean);

  const filterBar = (
    <ListFilters
      label="Filter risk assessments"
      fields={[
        { key: "repository", label: "Repository", placeholder: "owner/name", suggestions: [...new Set(rows.map((r) => r.repository))].sort() },
        { key: "risk_band", label: "Band", placeholder: "LOW, MEDIUM, HIGH or CRITICAL", suggestions: ["LOW", "MEDIUM", "HIGH", "CRITICAL"] },
      ]}
      value={filters}
      onChange={setFilters}
      storageKey={storageKey("risk", orgId)}
    />
  );

  if (query.isPending) return <LoadingState label="Loading risk assessments…" lines={6} />;
  if (query.isError) {
    if (isEndpointMissing(query.error)) {
      return (
        <>
          <Callout tone="info" title="Risk assessments arrive with M5" className="mb-6">
            This API build does not serve risk assessments yet. Nothing is hidden: once the endpoint exists, every scored change is
            listed here with its factors.
          </Callout>
          <RiskSetup title="Set up risk scoring" />
        </>
      );
    }
    return <ApiErrorState what="Risk assessments" error={query.error} onRetry={() => void query.refetch()} />;
  }
  if (rows.length === 0 && !filtered) return <RiskSetup />;
  return (
    <>
      {filterBar}
      <DataTable<RiskAssessment>
        caption="Risk assessments, newest first"
        columns={COLUMNS}
        rows={rows}
        getRowId={(a) => a.assessment_id}
        onRowActivate={(a) => router.push(`/app/risk/${a.assessment_id}`)}
        emptyState={<p className="text-center text-sm text-muted-foreground">No assessments match these filters.</p>}
      />
      <LoadMore
        hasNextPage={query.hasNextPage}
        isFetchingNextPage={query.isFetchingNextPage}
        fetchNextPage={() => void query.fetchNextPage()}
        shown={rows.length}
        noun={rows.length === 1 ? "assessment" : "assessments"}
      />
    </>
  );
}

function formatFact(value: unknown): string {
  if (value === null || value === undefined) return "unknown";
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "number") return Number.isInteger(value) ? value.toLocaleString("en-US") : value.toFixed(3);
  return String(value);
}

/** Every feature's SHAP contribution, largest magnitude first (the full vector, not only the top signals). */
export function shapRows(a: Pick<RiskAssessmentDetail, "shap" | "features" | "top_increasing" | "top_reducing">) {
  const labels = new Map([...a.top_increasing, ...a.top_reducing].map((s) => [s.feature, s.label]));
  return Object.entries(a.shap.values ?? {})
    .map(([feature, contribution]) => ({ feature, label: labels.get(feature) ?? feature, contribution, value: a.features[feature] }))
    .sort((x, y) => Math.abs(y.contribution) - Math.abs(x.contribution));
}

function ShapTable({ a }: { a: RiskAssessmentDetail }) {
  const rows = shapRows(a);
  if (rows.length === 0) return <p className="text-sm text-muted-foreground">The model returned no SHAP vector for this assessment.</p>;
  const max = Math.max(1e-9, ...rows.map((r) => Math.abs(r.contribution)));
  return (
    <div className="max-h-[28rem] overflow-auto">
      <table className="w-full text-sm">
        <caption className="sr-only">Every feature&apos;s value and SHAP contribution, largest first</caption>
        <thead>
          <tr className="text-xs text-muted-foreground">
            <th scope="col" className="sticky top-0 bg-panel py-1.5 text-left font-medium">Feature</th>
            <th scope="col" className="sticky top-0 bg-panel py-1.5 text-right font-medium">Value</th>
            <th scope="col" className="sticky top-0 bg-panel py-1.5 pl-3 text-left font-medium">Contribution ({a.shap.unit})</th>
          </tr>
        </thead>
        <tbody className="tabular-nums">
          {rows.map((r) => (
            <tr key={r.feature} className="border-t">
              <th scope="row" className="py-1.5 pr-3 text-left font-normal">
                {r.label}
                {r.label !== r.feature ? <span className="block font-mono text-[11px] text-muted-foreground">{r.feature}</span> : null}
              </th>
              <td className="py-1.5 text-right text-foreground-secondary">{formatFact(r.value)}</td>
              <td className="py-1.5 pl-3">
                <span className="flex items-center gap-2">
                  <span className="w-14 text-right text-xs">
                    {r.contribution >= 0 ? "+" : "−"}
                    {Math.abs(r.contribution).toFixed(3)}
                  </span>
                  <span aria-hidden="true" className="relative h-1.5 w-24 rounded-full bg-subtle">
                    <span className="absolute inset-y-0 left-1/2 w-px bg-border-strong" />
                    <span
                      className={`absolute inset-y-0 rounded-full ${r.contribution >= 0 ? "left-1/2 bg-high" : "right-1/2 bg-healthy"}`}
                      style={{ width: `${(Math.abs(r.contribution) / max) * 50}%` }}
                    />
                  </span>
                  <span className="sr-only">{r.contribution >= 0 ? "raised the score" : "lowered the score"}</span>
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {a.shap.base_value !== null && a.shap.base_value !== undefined ? (
        <p className="mt-2 text-xs text-muted-foreground">
          Base value {a.shap.base_value.toFixed(3)} ({a.shap.unit}); contributions add up to the model&apos;s raw score {a.raw_score.toFixed(3)}.
        </p>
      ) : null}
    </div>
  );
}

export function RiskDetailView({ assessmentId }: { assessmentId: string }) {
  const { orgId } = useSession();
  const query = useRiskAssessment(orgId, assessmentId);
  const crumbs = [
    { label: "Dashboard", href: "/app" },
    { label: "Risk analysis", href: "/app/risk" },
    { label: shortSha(assessmentId) },
  ];

  if (query.isPending) return <LoadingState label="Loading the assessment…" lines={6} />;
  if (query.isError) {
    return (
      <>
        <PageHeader title="Risk assessment" breadcrumbs={crumbs} />
        {isEndpointMissing(query.error) ? (
          <Callout tone="info" title="Risk assessments arrive with M5">
            Assessment <code className="font-mono text-xs">{assessmentId}</code> cannot be read yet: this API build does not serve risk
            assessments.
          </Callout>
        ) : (
          <ApiErrorState what="This assessment" error={query.error} onRetry={() => void query.refetch()} />
        )}
      </>
    );
  }
  const a = query.data;
  const facts = Object.entries(a.change_facts ?? {}).sort(([x], [y]) => x.localeCompare(y));
  return (
    <>
      <PageHeader
        title={`${a.repository}${a.pull_request ? ` #${a.pull_request}` : ""}`}
        description={`CI failure risk of commit ${shortSha(a.commit_sha)}, scored ${formatDateTime(a.created_at)} by ${a.model_version}.`}
        breadcrumbs={crumbs}
        meta={<RiskBadge score={a.risk_score} band={a.risk_band} />}
      />
      <RiskMeaning />
      {a.explanation_note ? (
        <Callout tone="info" className="mb-6">
          {a.explanation_note}
        </Callout>
      ) : null}
      <div className="grid gap-6 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <section aria-labelledby="factors-title" className="rounded-xl border bg-panel shadow-panel">
          <h2 id="factors-title" className="border-b px-5 py-3.5 text-sm font-semibold">
            What moved the score (SHAP)
          </h2>
          <div className="px-5 py-4">
            <ShapBars increasing={a.top_increasing} reducing={a.top_reducing} limit={10} />
          </div>
        </section>
        <div className="space-y-6">
          <div className="rounded-xl border bg-panel px-5 shadow-panel">
            <KeyValueList
              labelWidth="sm:grid-cols-[minmax(0,8rem)_minmax(0,1fr)]"
              items={[
                { label: "Score", value: `${a.risk_score} / 100 (${a.risk_band.toLowerCase()} band)` },
                {
                  label: "Probability",
                  value: a.probability !== null ? `${(a.probability * 100).toFixed(1)}% (calibrated)` : "Not exposed (calibration gate not passed)",
                },
                { label: "Commit", value: <span className="font-mono text-xs break-all">{a.commit_sha}</span> },
                { label: "Branch", value: a.branch ?? DASH },
                { label: "Event", value: a.event === "pull_request" ? "Pull request" : a.event === "push" ? "Push" : a.event },
                {
                  label: "History",
                  value: `${a.history.finished_builds_used} finished builds used${a.history.cold_start ? " (cold start: less reliable)" : ""}`,
                },
                { label: "Model", value: <code className="font-mono text-xs">{a.model_version}</code>, hint: `Feature schema ${a.feature_schema_version}` },
                ...(a.ci_run_url
                  ? [
                      {
                        label: "CI run",
                        value: (
                          <a href={a.ci_run_url} target="_blank" rel="noreferrer noopener" className="inline-flex items-center gap-1 text-brand-text hover:underline">
                            Open <ExternalLink aria-hidden="true" className="size-3.5" />
                            <span className="sr-only">(opens in a new tab)</span>
                          </a>
                        ),
                      },
                    ]
                  : []),
              ]}
            />
          </div>
          <section aria-labelledby="ci-title" className="rounded-xl border bg-panel px-5 py-4 shadow-panel">
            <h2 id="ci-title" className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
              CI result (separate signal)
            </h2>
            <CiRunSignal orgId={orgId} repository={a.repository} commitSha={a.commit_sha} ciRunUrl={a.ci_run_url} />
          </section>
        </div>
      </div>

      <div className="mt-6 grid gap-6 lg:grid-cols-2">
        <section aria-labelledby="shap-all-title" className="rounded-xl border bg-panel shadow-panel">
          <h2 id="shap-all-title" className="border-b px-5 py-3.5 text-sm font-semibold">
            Every feature&apos;s contribution
          </h2>
          <div className="px-5 py-4">
            <ShapTable a={a} />
          </div>
        </section>
        <section aria-labelledby="facts-title" className="rounded-xl border bg-panel shadow-panel">
          <h2 id="facts-title" className="border-b px-5 py-3.5 text-sm font-semibold">
            Change facts sent by the extractor
          </h2>
          <div className="px-5 py-4">
            {facts.length === 0 ? (
              <p className="text-sm text-muted-foreground">No change facts were sent; every change feature counted as unknown.</p>
            ) : (
              <dl className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-4 gap-y-1.5 text-sm">
                {facts.map(([k, v]) => (
                  <div key={k} className="contents">
                    <dt className="font-mono text-xs text-muted-foreground">{k}</dt>
                    <dd className="text-right tabular-nums">{formatFact(v)}</dd>
                  </div>
                ))}
              </dl>
            )}
            <p className="mt-3 text-xs text-muted-foreground">
              A missing fact is treated as unknown, never as 0.
              {a.extractor && typeof a.extractor === "object" && "name" in a.extractor ? ` Extractor: ${String(a.extractor.name)} ${String((a.extractor as Record<string, unknown>).version ?? "")}.` : ""}
            </p>
          </div>
        </section>
      </div>
    </>
  );
}

"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo } from "react";

import { VersionChange } from "@/components/deployments/deployments-view";
import { AnalysisStatusPill, LoadMore } from "@/components/rl/analysis-status";
import { ApiErrorState } from "@/components/rl/api-error-state";
import { ListFilters, useUrlFilters } from "@/components/rl/list-filters";
import { RegressionsSetup } from "@/components/rl/setup-empty-states";
import { SeverityBadge } from "@/components/rl/severity-badge";
import { useSession } from "@/components/session/session-provider";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { LoadingState } from "@/components/ui/skeleton";
import { CONFIDENCE_LABEL, KIND_LABEL } from "@/lib/analysis";
import { useRegressions } from "@/lib/api/queries";
import type { RegressionItem } from "@/lib/api/types";
import { formatDateTime, formatRelativeTime } from "@/lib/format";
import { storageKey } from "@/lib/saved-views";

const SEVERITY_RANK: Record<string, number> = { HEALTHY: 0, WARNING: 1, HIGH: 2, CRITICAL: 3 };

export const REGRESSION_COLUMNS: DataTableColumn<RegressionItem>[] = [
  {
    id: "service",
    header: "Deployed service",
    rowHeader: true,
    sortValue: (r) => r.service,
    cell: (r) => (
      <Link href={`/app/deployments/${r.deployment_id}`} tabIndex={-1} className="rounded-sm hover:underline">
        {r.service}
      </Link>
    ),
  },
  { id: "version", header: "Version", cell: (r) => <VersionChange previous={r.previous_version} next={r.version} /> },
  { id: "environment", header: "Environment", sortValue: (r) => r.environment, cell: (r) => r.environment },
  { id: "status", header: "Verdict", sortValue: (r) => r.status, cell: (r) => <AnalysisStatusPill status={r.status} size="sm" /> },
  {
    id: "severity",
    header: "Severity",
    sortValue: (r) => (r.severity_band ? (SEVERITY_RANK[r.severity_band] ?? 0) * 1000 + (r.severity_score ?? 0) : null),
    cell: (r) => <SeverityBadge severity={r.severity_band} score={r.severity_score} size="sm" />,
  },
  {
    id: "origin",
    header: "Probable origin",
    sortValue: (r) => r.probable_origin,
    cell: (r) =>
      r.probable_origin ? (
        <span>
          <span className="font-medium text-foreground">{r.probable_origin}</span>
          {r.confidence ? <span className="block text-[11px] text-muted-foreground">{CONFIDENCE_LABEL[r.confidence]}</span> : null}
        </span>
      ) : (
        <span className="text-xs text-muted-foreground">Not named{r.confidence ? ` (${CONFIDENCE_LABEL[r.confidence].toLowerCase()})` : ""}</span>
      ),
  },
  {
    id: "affected",
    header: "Affected",
    sortValue: (r) => r.affected_services.length,
    cell: (r) => (r.affected_services.length ? r.affected_services.join(", ") : <span className="text-xs text-muted-foreground">None</span>),
  },
  {
    id: "analysed",
    header: "Deployed",
    sortValue: (r) => new Date(r.deployed_at),
    cell: (r) => (
      <span className="text-xs">
        <time dateTime={r.deployed_at} title={formatDateTime(r.deployed_at)}>
          {formatRelativeTime(r.deployed_at)}
        </time>
        <span className="block text-muted-foreground">{KIND_LABEL[r.analysis_kind]}</span>
      </span>
    ),
  },
];

export function RegressionsView() {
  const { orgId } = useSession();
  const router = useRouter();
  const [filters, setFilters] = useUrlFilters(["service", "environment"]);
  const query = useRegressions(orgId, filters);
  const rows = useMemo(() => query.data?.pages.flatMap((p) => p.items) ?? [], [query.data]);
  const filtered = Object.values(filters).some(Boolean);

  return (
    <>
      <ListFilters
        label="Filter regressions"
        fields={[
          { key: "service", label: "Deployed service", suggestions: [...new Set(rows.map((r) => r.service))].sort() },
          { key: "environment", label: "Environment", suggestions: [...new Set(rows.map((r) => r.environment))].sort() },
        ]}
        value={filters}
        onChange={setFilters}
        storageKey={storageKey("regressions", orgId)}
      />
      {query.isPending ? (
        <LoadingState label="Loading regressions…" lines={6} />
      ) : query.isError ? (
        <ApiErrorState what="Regressions" error={query.error} onRetry={() => void query.refetch()} retrying={query.isRefetching} />
      ) : rows.length === 0 && !filtered ? (
        <RegressionsSetup />
      ) : (
        <>
          <DataTable<RegressionItem>
            caption="Deployments whose latest analysis is Regressed or Warning, newest first"
            columns={REGRESSION_COLUMNS}
            rows={rows}
            getRowId={(r) => r.analysis_id}
            onRowActivate={(r) => router.push(`/app/deployments/${r.deployment_id}`)}
            emptyState={<p className="text-center text-sm text-muted-foreground">No regressions match these filters.</p>}
          />
          <LoadMore
            hasNextPage={query.hasNextPage}
            isFetchingNextPage={query.isFetchingNextPage}
            fetchNextPage={() => void query.fetchNextPage()}
            shown={rows.length}
            noun={rows.length === 1 ? "regression" : "regressions"}
          />
        </>
      )}
    </>
  );
}

"use client";

import Link from "next/link";
import { useMemo } from "react";

import { LoadMore } from "@/components/rl/analysis-status";
import { ApiErrorState } from "@/components/rl/api-error-state";
import { ListFilters, useUrlFilters } from "@/components/rl/list-filters";
import { ServicesSetup } from "@/components/rl/setup-empty-states";
import { useSession } from "@/components/session/session-provider";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { LoadingState } from "@/components/ui/skeleton";
import { useServices } from "@/lib/api/queries";
import type { ServiceHealth, ServiceItem } from "@/lib/api/types";
import { DASH, formatCount, formatDateTime, formatMs, formatRate, formatRelativeTime } from "@/lib/format";

/** "≈ 212 ms (200–250 ms bucket)": the catalog's p95 comes from a histogram and is approximate. */
export function approxP95(health: ServiceHealth): string {
  if (health.latency_p95_ms_approx === null) return DASH;
  return `≈ ${formatMs(health.latency_p95_ms_approx)}`;
}

export function p95Bucket(health: ServiceHealth): string | null {
  const bucket = health.latency_p95_bucket_ms;
  if (!bucket || bucket.length < 2) return null;
  const [low, high] = bucket;
  if (low === null || low === undefined) return null;
  return high === null || high === undefined ? `above ${formatMs(low)}` : `${formatMs(low)}–${formatMs(high)}`;
}

export const SERVICE_COLUMNS: DataTableColumn<ServiceItem>[] = [
  { id: "service", header: "Service", rowHeader: true, sortValue: (s) => s.service, cell: (s) => s.service },
  { id: "environment", header: "Environment", sortValue: (s) => s.environment, cell: (s) => s.environment ?? <span className="text-muted-foreground">Not set</span> },
  {
    id: "version",
    header: "Latest version seen",
    sortValue: (s) => s.latest_version_seen,
    cell: (s) =>
      s.latest_version_seen ? (
        <span className="font-mono text-xs" title={s.latest_version_seen_at ? `Seen ${formatDateTime(s.latest_version_seen_at)}` : undefined}>
          {s.latest_version_seen}
        </span>
      ) : (
        DASH
      ),
  },
  {
    id: "rate",
    header: "Requests/min",
    align: "right",
    sortValue: (s) => s.health.request_rate_per_min,
    cell: (s) => (s.health.requests === 0 ? <span className="text-xs text-muted-foreground">No traffic</span> : s.health.request_rate_per_min.toFixed(1)),
  },
  {
    id: "errors",
    header: "Error rate",
    align: "right",
    sortValue: (s) => s.health.error_rate,
    cell: (s) => (
      <span title={`${formatCount(s.health.errors)} of ${formatCount(s.health.requests)} requests`}>{formatRate(s.health.error_rate)}</span>
    ),
  },
  {
    id: "p95",
    header: "p95 latency",
    align: "right",
    sortValue: (s) => s.health.latency_p95_ms_approx,
    cell: (s) => {
      const bucket = p95Bucket(s.health);
      return (
        <span>
          {approxP95(s.health)}
          {bucket ? <span className="block text-[11px] text-muted-foreground">bucket {bucket}</span> : null}
        </span>
      );
    },
  },
  {
    id: "deploy",
    header: "Last deployment",
    sortValue: (s) => (s.last_deployment ? new Date(s.last_deployment.deployed_at) : null),
    cell: (s) =>
      s.last_deployment ? (
        <Link href={`/app/deployments/${s.last_deployment.id}`} className="rounded-sm text-brand-text hover:underline" tabIndex={-1}>
          <span className="font-mono text-xs">{s.last_deployment.version}</span>{" "}
          <span className="text-xs">{formatRelativeTime(s.last_deployment.deployed_at)}</span>
        </Link>
      ) : (
        <span className="text-xs text-muted-foreground">None reported</span>
      ),
  },
];

export function ServicesView() {
  const { orgId } = useSession();
  const [filters, setFilters] = useUrlFilters(["environment"]);
  const query = useServices(orgId, filters.environment);
  const rows = useMemo(() => query.data?.pages.flatMap((p) => p.items) ?? [], [query.data]);
  const healthWindow = rows[0]?.health;

  return (
    <>
      <ListFilters
        label="Filter services"
        fields={[{ key: "environment", label: "Environment", suggestions: [...new Set(rows.map((r) => r.environment).filter((e): e is string => Boolean(e)))].sort() }]}
        value={filters}
        onChange={setFilters}
      />
      {query.isPending ? (
        <LoadingState label="Loading services…" lines={6} />
      ) : query.isError ? (
        <ApiErrorState what="The service catalog" error={query.error} onRetry={() => void query.refetch()} retrying={query.isRefetching} />
      ) : rows.length === 0 && !filters.environment ? (
        <ServicesSetup />
      ) : (
        <>
          <DataTable<ServiceItem>
            caption="Services reporting traces"
            columns={SERVICE_COLUMNS}
            rows={rows}
            getRowId={(s) => `${s.service}|${s.environment ?? ""}`}
            initialSort={{ columnId: "service", direction: "ascending" }}
            emptyState={<p className="text-center text-sm text-muted-foreground">No services in this environment.</p>}
          />
          <p className="mt-2 text-xs text-muted-foreground">
            Rates and latency cover the last 15 complete minutes
            {healthWindow ? ` (${formatDateTime(healthWindow.window_start)} – ${formatDateTime(healthWindow.window_end)})` : ""}. p95 is approximate: read from
            a log-scale histogram of minute rollups, with the bucket that holds the exact value.
          </p>
          <LoadMore
            hasNextPage={query.hasNextPage}
            isFetchingNextPage={query.isFetchingNextPage}
            fetchNextPage={() => void query.fetchNextPage()}
            shown={rows.length}
            noun={rows.length === 1 ? "service" : "services"}
          />
        </>
      )}
    </>
  );
}

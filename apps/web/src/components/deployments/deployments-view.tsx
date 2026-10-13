"use client";

import { ArrowRight, Link2 } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo } from "react";

import { LoadMore } from "@/components/rl/analysis-status";
import { ApiErrorState } from "@/components/rl/api-error-state";
import { ListFilters, useUrlFilters } from "@/components/rl/list-filters";
import { DeploymentsSetup } from "@/components/rl/setup-empty-states";
import { useSession } from "@/components/session/session-provider";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { LoadingState } from "@/components/ui/skeleton";
import { useDeployments } from "@/lib/api/queries";
import type { DeploymentOut } from "@/lib/api/types";
import { DASH, formatDateTime, formatRelativeTime, shortSha } from "@/lib/format";
import { storageKey } from "@/lib/saved-views";

export function VersionChange({ previous, next }: { previous: string | null; next: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 font-mono text-xs">
      <span className="text-muted-foreground">{previous ?? DASH}</span>
      <ArrowRight aria-label="to" className="size-3 text-muted-foreground" />
      <span className="text-foreground">{next}</span>
    </span>
  );
}

const COLUMNS: DataTableColumn<DeploymentOut>[] = [
  {
    id: "service",
    header: "Service",
    rowHeader: true,
    sortValue: (d) => d.service,
    cell: (d) => (
      <Link href={`/app/deployments/${d.id}`} className="rounded-sm hover:underline" tabIndex={-1}>
        {d.service}
      </Link>
    ),
  },
  { id: "version", header: "Version", cell: (d) => <VersionChange previous={d.previous_version} next={d.version} /> },
  { id: "environment", header: "Environment", sortValue: (d) => d.environment, cell: (d) => d.environment },
  {
    id: "deployed",
    header: "Deployed",
    sortValue: (d) => new Date(d.deployed_at),
    cell: (d) => (
      <time dateTime={d.deployed_at} title={formatDateTime(d.deployed_at)}>
        {formatRelativeTime(d.deployed_at)}
      </time>
    ),
  },
  { id: "commit", header: "Commit", cell: (d) => <code className="font-mono text-xs">{shortSha(d.commit_sha)}</code> },
  { id: "deployer", header: "Deployer", cell: (d) => d.deployer ?? DASH },
  {
    id: "risk",
    header: "Risk check",
    cell: (d) =>
      d.risk_assessment_id ? (
        <span className="inline-flex items-center gap-1 text-xs">
          <Link2 aria-hidden="true" className="size-3.5" />
          Linked
        </span>
      ) : (
        <span className="text-xs text-muted-foreground">None</span>
      ),
  },
];

export function DeploymentsView() {
  const { orgId } = useSession();
  const router = useRouter();
  const [filters, setFilters] = useUrlFilters(["service", "environment"]);
  const query = useDeployments(orgId, filters);
  const rows = useMemo(() => query.data?.pages.flatMap((p) => p.items) ?? [], [query.data]);
  const suggestions = useMemo(
    () => ({
      service: [...new Set(rows.map((r) => r.service))].sort(),
      environment: [...new Set(rows.map((r) => r.environment))].sort(),
    }),
    [rows],
  );
  const filtered = Object.values(filters).some(Boolean);

  return (
    <>
      <ListFilters
        label="Filter deployments"
        fields={[
          { key: "service", label: "Service", suggestions: suggestions.service },
          { key: "environment", label: "Environment", suggestions: suggestions.environment },
        ]}
        value={filters}
        onChange={setFilters}
        storageKey={storageKey("deployments", orgId)}
      />
      {query.isPending ? (
        <LoadingState label="Loading deployments…" lines={6} />
      ) : query.isError ? (
        <ApiErrorState what="Deployments" error={query.error} onRetry={() => void query.refetch()} retrying={query.isRefetching} />
      ) : rows.length === 0 && !filtered ? (
        <DeploymentsSetup />
      ) : (
        <>
          <DataTable<DeploymentOut>
            caption="Deployments, newest first"
            columns={COLUMNS}
            rows={rows}
            getRowId={(d) => d.id}
            onRowActivate={(d) => router.push(`/app/deployments/${d.id}`)}
            emptyState={<p className="text-center text-sm text-muted-foreground">No deployments match these filters.</p>}
          />
          <LoadMore
            hasNextPage={query.hasNextPage}
            isFetchingNextPage={query.isFetchingNextPage}
            fetchNextPage={() => void query.fetchNextPage()}
            shown={rows.length}
            noun={rows.length === 1 ? "deployment" : "deployments"}
          />
        </>
      )}
    </>
  );
}

"use client";

import { Download } from "lucide-react";
import { useMemo, useState } from "react";

import { LoadMore } from "@/components/rl/analysis-status";
import { ApiErrorState } from "@/components/rl/api-error-state";
import { ListFilters, useUrlFilters } from "@/components/rl/list-filters";
import { useSession } from "@/components/session/session-provider";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { LoadingState } from "@/components/ui/skeleton";
import { apiFetch } from "@/lib/api/client";
import { describeApiError, parseApiError } from "@/lib/api/errors";
import { useAuditLog, type AuditFilters } from "@/lib/api/queries";
import type { AuditEntryOut } from "@/lib/api/types";
import { API_BASE_URL } from "@/lib/env";
import { DASH, formatDateTime } from "@/lib/format";

const FILTER_KEYS = ["action", "actor_user_id", "target_type", "target_id", "since", "until"] as const;

export const AUDIT_ACTIONS = [
  "auth.login_succeeded",
  "auth.login_failed",
  "org.created",
  "org.settings_changed",
  "member.invited",
  "member.joined",
  "member.role_changed",
  "member.removed",
  "api_key.created",
  "api_key.revoked",
  "api_key.rotated",
  "project.created",
  "project.updated",
  "project.deleted",
  "alert_rule.created",
  "alert_rule.updated",
  "alert_rule.deleted",
  "billing.checkout_started",
  "billing.subscription_changed",
  "billing.payment_failed",
  "model.promoted",
];

/** The export URL for the same filters (≤ 50,000 rows, formula-injection safe CSV). */
export function auditExportUrl(orgId: string, filters: AuditFilters, base = API_BASE_URL): string {
  const url = new URL(`${base}/api/v1/orgs/${encodeURIComponent(orgId)}/audit-log/export.csv`);
  for (const [k, v] of Object.entries(filters)) if (v) url.searchParams.set(k, v);
  return url.toString();
}

const COLUMNS: DataTableColumn<AuditEntryOut>[] = [
  { id: "when", header: "When", sortValue: (e) => new Date(e.created_at), cell: (e) => <time dateTime={e.created_at}>{formatDateTime(e.created_at)}</time> },
  { id: "action", header: "Action", rowHeader: true, sortValue: (e) => e.action, cell: (e) => <code className="font-mono text-xs">{e.action}</code> },
  {
    id: "actor",
    header: "Actor",
    cell: (e) => (
      <span className="text-xs">
        {e.actor_email ?? (e.actor_api_key_id ? `API key ${e.actor_api_key_id.slice(0, 8)}` : e.actor_type)}
        <span className="block text-muted-foreground">{e.actor_type}</span>
      </span>
    ),
  },
  {
    id: "target",
    header: "Target",
    cell: (e) => (e.target_type ? <span className="text-xs">{e.target_type}{e.target_id ? ` ${e.target_id.slice(0, 8)}…` : ""}</span> : DASH),
  },
  { id: "ip", header: "IP", cell: (e) => <span className="font-mono text-xs">{e.ip ?? DASH}</span> },
  {
    id: "details",
    header: "Details",
    cell: (e) => {
      const entries = Object.entries(e.metadata ?? {});
      return entries.length ? (
        <span className="line-clamp-2 max-w-xs text-xs text-muted-foreground" title={JSON.stringify(e.metadata)}>
          {entries.map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : String(v)}`).join(" · ")}
        </span>
      ) : (
        DASH
      );
    },
  },
];

export function AuditLogSettings() {
  const session = useSession();
  const orgId = session.orgId!;
  const [filters, setFilters] = useUrlFilters([...FILTER_KEYS]);
  const query = useAuditLog(orgId, filters);
  const rows = useMemo(() => query.data?.pages.flatMap((p) => p.items) ?? [], [query.data]);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);

  const exportCsv = async () => {
    setExporting(true);
    setExportError(null);
    try {
      // Through the API client, so an expired access token is refreshed first.
      const response = await apiFetch(auditExportUrl(orgId, filters), { headers: { accept: "text/csv" } });
      if (!response.ok) throw parseApiError(response.status, await response.json().catch(() => undefined), response.headers);
      const blob = await response.blob();
      const href = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = href;
      a.download = `audit-log-${new Date().toISOString().slice(0, 10)}.csv`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(href), 1000);
    } catch (error) {
      setExportError(describeApiError(error));
    } finally {
      setExporting(false);
    }
  };

  if (!session.can("admin")) {
    return <Callout tone="info" title="Owners and admins only">The audit log and its CSV export are visible to owners and admins.</Callout>;
  }

  return (
    <div>
      <ListFilters
        label="Filter the audit log"
        fields={[
          { key: "action", label: "Action", placeholder: "e.g. api_key.created", suggestions: AUDIT_ACTIONS },
          { key: "target_type", label: "Target type", placeholder: "e.g. api_key" },
          { key: "actor_user_id", label: "Actor user id" },
          { key: "since", label: "Since (ISO 8601)", placeholder: "2026-10-01T00:00:00Z" },
          { key: "until", label: "Until (ISO 8601)", placeholder: "2026-10-13T00:00:00Z" },
        ]}
        value={filters}
        onChange={setFilters}
      />
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <Button variant="outline" size="sm" onClick={() => void exportCsv()} disabled={exporting}>
          <Download aria-hidden="true" />
          {exporting ? "Preparing CSV…" : "Export CSV"}
        </Button>
        <span className="text-xs text-muted-foreground">Same filters, up to 50,000 rows. Entries are append-only.</span>
        {exportError ? (
          <span role="alert" className="text-xs font-medium text-critical">
            {exportError}
          </span>
        ) : null}
      </div>
      {query.isPending ? (
        <LoadingState label="Loading the audit log…" lines={6} />
      ) : query.isError ? (
        <ApiErrorState what="The audit log" error={query.error} onRetry={() => void query.refetch()} />
      ) : (
        <>
          <DataTable<AuditEntryOut>
            caption="Audit log, newest first"
            columns={COLUMNS}
            rows={rows}
            getRowId={(e) => e.id}
            emptyState={<p className="text-center text-sm text-muted-foreground">No entries match these filters.</p>}
          />
          <LoadMore
            hasNextPage={query.hasNextPage}
            isFetchingNextPage={query.isFetchingNextPage}
            fetchNextPage={() => void query.fetchNextPage()}
            shown={rows.length}
            noun={rows.length === 1 ? "entry" : "entries"}
          />
        </>
      )}
    </div>
  );
}

"use client";

import { StatusPill } from "@/components/rl/status-pill";
import { useOptionalSession } from "@/components/session/session-provider";
import { describeApiError } from "@/lib/api/errors";
import { useRiskModelStatus } from "@/lib/api/queries";
import { formatDateTime } from "@/lib/format";

/** Which model versions this API process actually serves (GET /api/v1/risk/model). */
export function ServingStatus() {
  const session = useOptionalSession();
  const status = useRiskModelStatus(session?.orgId);
  if (status.isPending) return null;
  if (status.isError) {
    return <p className="text-xs text-muted-foreground">Serving status unavailable: {describeApiError(status.error)}</p>;
  }
  const loaded = Object.entries(status.data.loaded);
  if (loaded.length === 0) {
    return <StatusPill tone="warning" label="The API has no risk model loaded: scoring answers 503" size="sm" />;
  }
  return (
    <ul className="flex flex-wrap gap-2 text-xs text-muted-foreground">
      {loaded.map(([role, info]) => (
        <li key={role} className="inline-flex items-center gap-2">
          <StatusPill tone="healthy" label={`Serving ${role}`} size="sm" />
          <span>
            <span className="font-mono text-foreground-secondary">{info.model_version}</span> · {info.features} features · loaded{" "}
            {formatDateTime(info.loaded_at)}
            {info.probability_exposed ? " · probability exposed" : ""}
          </span>
        </li>
      ))}
    </ul>
  );
}

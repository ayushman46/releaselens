"use client";

import { ExternalLink } from "lucide-react";

import { StatusPill } from "@/components/rl/status-pill";
import { LoadingState } from "@/components/ui/skeleton";
import { describeApiError } from "@/lib/api/errors";
import { useCiRuns } from "@/lib/api/queries";
import { formatDateTime } from "@/lib/format";
import type { Tone } from "@/lib/tones";

const STATUS_TONE: Record<string, { tone: Tone; label: string }> = {
  passed: { tone: "healthy", label: "CI passed" },
  failed: { tone: "critical", label: "CI failed" },
  errored: { tone: "high", label: "CI errored" },
  canceled: { tone: "unknown", label: "CI canceled" },
  started: { tone: "info", label: "CI running" },
  running: { tone: "info", label: "CI running" },
};

export function ciStatusMeta(status: string): { tone: Tone; label: string } {
  return STATUS_TONE[status] ?? { tone: "unknown", label: `CI ${status}` };
}

/**
 * The actual CI outcome of a commit, from the CI runs reported to ReleaseLens. Shown next to the
 * risk score as a separate signal; the model never uses it (it would leak the answer).
 */
export function CiRunSignal({
  orgId,
  repository,
  commitSha,
  ciRunUrl,
}: {
  orgId: string | undefined;
  repository: string | null | undefined;
  commitSha: string;
  ciRunUrl?: string | null;
}) {
  const runs = useCiRuns(orgId, repository);
  const link = ciRunUrl ? (
    <a href={ciRunUrl} target="_blank" rel="noreferrer noopener" className="inline-flex items-center gap-1 text-xs font-medium text-brand-text hover:underline">
      Open the CI run
      <ExternalLink aria-hidden="true" className="size-3.5" />
      <span className="sr-only">(opens in a new tab)</span>
    </a>
  ) : null;

  if (!repository) {
    return (
      <div className="mt-1.5 space-y-1 text-sm text-muted-foreground">
        <p>No repository was sent, so the CI outcome cannot be matched.</p>
        {link}
      </div>
    );
  }
  if (runs.isPending) return <LoadingState className="mt-2" label="Loading CI runs…" lines={1} />;
  if (runs.isError) {
    return (
      <div className="mt-1.5 space-y-1 text-sm text-muted-foreground">
        <p>CI runs could not be loaded: {describeApiError(runs.error)}</p>
        {link}
      </div>
    );
  }
  const sha = commitSha.toLowerCase();
  const run = runs.data.find((r) => r.commit_sha === sha);
  if (!run) {
    return (
      <div className="mt-1.5 space-y-1 text-sm text-muted-foreground">
        <p>No CI run was reported for commit {sha.slice(0, 7)} yet (POST /api/v1/ci-runs).</p>
        {link}
      </div>
    );
  }
  const meta = ciStatusMeta(run.status);
  return (
    <div className="mt-1.5 flex flex-wrap items-center gap-2 text-sm">
      <StatusPill tone={meta.tone} label={meta.label} size="sm" />
      <span className="text-xs text-muted-foreground">
        {run.finished_at ? `finished ${formatDateTime(run.finished_at)}` : `started ${formatDateTime(run.started_at)}`}
        {run.duration_s !== null ? ` · ${Math.round(run.duration_s)} s` : ""} · {run.source}
      </span>
      {link}
    </div>
  );
}

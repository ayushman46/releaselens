"use client";

import { useQueryClient } from "@tanstack/react-query";
import { RefreshCw, Unplug } from "lucide-react";

import { StatusBadge, StatusIcon } from "@/components/status/status-badge";
import {
  HEALTH_REFETCH_INTERVAL_MS,
  healthQueryKeys,
  useApiHealth,
} from "@/components/status/use-api-health";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  dependencyLabel,
  formatLatency,
  HealthApiError,
  sortedChecks,
  type DependencyCheck,
} from "@/lib/api/health";
import { API_BASE_URL } from "@/lib/env";
import { livenessState, overallState, readinessState, type HealthState } from "@/lib/status";
import { cn } from "@/lib/utils";

/** Shown while the first readiness response is in flight (docs/dev/CONVENTIONS.md §4). */
const EXPECTED_DEPENDENCIES = ["postgres", "kafka"];

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return "Unknown error";
}

export function StatusDashboard({ baseUrl = API_BASE_URL }: { baseUrl?: string }) {
  const queryClient = useQueryClient();
  const { liveness, readiness } = useApiHealth(baseUrl);

  const overall = overallState(liveness, readiness);
  const isFetching = liveness.isFetching || readiness.isFetching;
  const lastChecked = Math.max(
    liveness.dataUpdatedAt,
    liveness.errorUpdatedAt,
    readiness.dataUpdatedAt,
    readiness.errorUpdatedAt,
  );
  const requestId =
    readiness.error instanceof HealthApiError
      ? readiness.error.requestId
      : readiness.isSuccess
        ? readiness.data.requestId
        : undefined;
  const unreachableError =
    overall === "unreachable" ? (liveness.error ?? readiness.error) : undefined;

  const refresh = () => {
    void queryClient.refetchQueries({ queryKey: healthQueryKeys.all });
  };

  return (
    <div className="mx-auto max-w-5xl px-4 py-10 sm:px-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold tracking-tight">System status</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Live probes against the ReleaseLens API at{" "}
            <code className="rounded border bg-muted px-1 py-0.5 font-mono text-xs text-foreground">
              {baseUrl}
            </code>
            , refreshed every {HEALTH_REFETCH_INTERVAL_MS / 1000} s.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <div aria-live="polite" aria-atomic="true">
            <span className="sr-only">API status: </span>
            <StatusBadge state={overall} />
          </div>
          <Button variant="outline" size="sm" onClick={refresh} disabled={isFetching}>
            <RefreshCw
              aria-hidden="true"
              className={cn(isFetching && "animate-spin motion-reduce:animate-none")}
            />
            Refresh
          </Button>
        </div>
      </div>

      {unreachableError !== undefined ? (
        <UnreachableState baseUrl={baseUrl} error={unreachableError} onRetry={refresh} retrying={isFetching} />
      ) : (
        <div className="mt-8 grid gap-4 md:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
          <Card className="gap-0 py-0">
            <CardHeader className="border-b py-4 [.border-b]:pb-4">
              <CardTitle className="text-sm">API</CardTitle>
              <CardDescription className="text-xs">Liveness and readiness probes</CardDescription>
            </CardHeader>
            <CardContent className="px-0">
              <ul className="divide-y">
                <ProbeRow name="Liveness" path="/healthz" state={livenessState(liveness)} error={liveness.error} />
                <ProbeRow
                  name="Readiness"
                  path="/readyz"
                  state={readinessState(readiness)}
                  error={readiness.error}
                />
              </ul>
            </CardContent>
          </Card>

          <Card className="gap-0 py-0">
            <CardHeader className="border-b py-4 [.border-b]:pb-4">
              <CardTitle className="text-sm">Dependencies</CardTitle>
              <CardDescription className="text-xs">
                Checked by the API on every readiness probe
              </CardDescription>
              {readiness.isSuccess ? (
                <CardAction>
                  <span className="text-xs text-muted-foreground tabular-nums">
                    HTTP {readiness.data.httpStatus}
                  </span>
                </CardAction>
              ) : null}
            </CardHeader>
            <CardContent className="px-0">
              <DependencyTable
                state={readinessState(readiness)}
                checks={readiness.isError ? undefined : readiness.data?.data.checks}
                error={readiness.error}
              />
            </CardContent>
          </Card>
        </div>
      )}

      {lastChecked > 0 ? (
        <p className="mt-4 text-xs text-muted-foreground tabular-nums">
          Last checked {new Date(lastChecked).toLocaleTimeString()}
          {requestId ? (
            <>
              {" · "}request ID <span className="font-mono">{requestId}</span>
            </>
          ) : null}
        </p>
      ) : null}
    </div>
  );
}

function ProbeRow({
  name,
  path,
  state,
  error,
}: {
  name: string;
  path: string;
  state: HealthState;
  error: unknown;
}) {
  return (
    <li className="px-6 py-3">
      <div className="flex items-center justify-between gap-4">
        <div className="min-w-0">
          <div className="text-sm font-medium">{name}</div>
          <div className="font-mono text-xs text-muted-foreground">GET {path}</div>
        </div>
        <StatusBadge state={state} />
      </div>
      {state === "error" && error ? (
        <p className="mt-2 text-xs break-words text-danger">{errorMessage(error)}</p>
      ) : null}
    </li>
  );
}

function DependencyTable({
  state,
  checks,
  error,
}: {
  state: HealthState;
  checks: Record<string, DependencyCheck> | undefined;
  error: unknown;
}) {
  if (state === "error") {
    return (
      <div role="alert" className="flex items-start gap-2 px-6 py-4 text-sm">
        <StatusIcon state="error" className="mt-0.5 size-4" />
        <div className="min-w-0">
          <p className="font-medium">Could not read readiness</p>
          <p className="mt-0.5 text-xs break-words text-muted-foreground">{errorMessage(error)}</p>
        </div>
      </div>
    );
  }

  const rows: Array<[string, DependencyCheck | undefined]> = checks
    ? sortedChecks(checks)
    : EXPECTED_DEPENDENCIES.map((name) => [name, undefined]);

  if (rows.length === 0) {
    return <p className="px-6 py-4 text-sm text-muted-foreground">The API reported no dependency checks.</p>;
  }

  return (
    <table className="w-full text-sm">
      <caption className="sr-only">Dependency checks reported by GET /readyz</caption>
      <thead>
        <tr className="border-b text-left text-xs text-muted-foreground">
          <th scope="col" className="px-6 py-2 font-medium">
            Dependency
          </th>
          <th scope="col" className="px-3 py-2 font-medium">
            Status
          </th>
          <th scope="col" className="px-6 py-2 text-right font-medium">
            Latency
          </th>
        </tr>
      </thead>
      <tbody className="divide-y">
        {rows.map(([name, check]) => (
          <tr key={name} className="align-top">
            <th scope="row" className="px-6 py-3 text-left font-medium">
              {dependencyLabel(name)}
              {check?.error ? (
                <span className="mt-1 block font-mono text-xs font-normal break-words text-danger">
                  {check.error}
                </span>
              ) : null}
            </th>
            <td className="px-3 py-3">
              <StatusBadge state={check ? check.status : "checking"} />
            </td>
            <td className="px-6 py-3 text-right font-mono text-xs text-muted-foreground tabular-nums">
              {check ? formatLatency(check.latency_ms) : "—"}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function UnreachableState({
  baseUrl,
  error,
  onRetry,
  retrying,
}: {
  baseUrl: string;
  error: unknown;
  onRetry: () => void;
  retrying: boolean;
}) {
  return (
    <Card role="alert" className="mt-8 items-center px-6 py-12 text-center">
      <div className="flex size-10 items-center justify-center rounded-full border bg-muted">
        <Unplug aria-hidden="true" className="size-5 text-danger" />
      </div>
      <div className="max-w-md space-y-2">
        <h2 className="text-base font-semibold">API unreachable</h2>
        <p className="text-sm text-muted-foreground">
          The browser could not get a response from{" "}
          <span className="font-mono text-xs text-foreground">{baseUrl}</span>. Check that the{" "}
          <span className="font-mono text-xs">api</span> service is running and that{" "}
          <span className="font-mono text-xs">RL_CORS_ORIGINS</span> allows this origin.
        </p>
        <p className="font-mono text-xs break-words text-muted-foreground">{errorMessage(error)}</p>
      </div>
      <Button variant="outline" size="sm" onClick={onRetry} disabled={retrying}>
        <RefreshCw
          aria-hidden="true"
          className={cn(retrying && "animate-spin motion-reduce:animate-none")}
        />
        {retrying ? "Retrying…" : "Retry"}
      </Button>
    </Card>
  );
}

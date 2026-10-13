"use client";

import { ArrowDownRight, ArrowUpRight, CircleCheck, Minus, OctagonAlert, TriangleAlert } from "lucide-react";
import { useId } from "react";

import { AnalysisStatusPill } from "@/components/rl/analysis-status";
import { SeverityBadge } from "@/components/rl/severity-badge";
import { Select } from "@/components/ui/field";
import { beforeAfterRows } from "@/lib/analysis";
import type { ServiceEvidence } from "@/lib/api/types";
import { formatCount } from "@/lib/format";

const DIRECTION_ICON = { up: ArrowUpRight, down: ArrowDownRight, flat: Minus } as const;
const DIRECTION_WORD = { up: "Increase", down: "Decrease", flat: "No change" } as const;

/**
 * p50/p95/p99 latency, error rate and throughput before and after, with the change, its interval
 * (or test) and the sample sizes, exactly as the regression engine computed them.
 */
export function BeforeAfterTable({
  services,
  selected,
  onSelect,
  origin,
}: {
  services: ServiceEvidence[];
  selected: ServiceEvidence;
  onSelect?: (service: string) => void;
  origin?: string | null;
}) {
  const selectId = useId();
  const rows = beforeAfterRows(selected);
  // Method notes once, under the table, instead of in every row.
  const notes = [...new Set(rows.map((r) => (r.key.startsWith("latency") ? r.note?.replace(/ of the p\d+(\.\d+)? difference/, " of each percentile difference") : r.note)).filter((n): n is string => Boolean(n)))];
  const gate = selected.sample_gate;

  return (
    <section aria-labelledby={`${selectId}-title`} className="rounded-xl border bg-panel shadow-panel">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b px-5 py-3.5">
        <h2 id={`${selectId}-title`} className="text-sm font-semibold">
          Before and after
        </h2>
        {services.length > 1 && onSelect ? (
          <div className="flex items-center gap-2">
            <label htmlFor={selectId} className="text-xs text-muted-foreground">
              Service
            </label>
            <Select id={selectId} value={selected.service} onChange={(e) => onSelect(e.target.value)} className="h-8 w-auto text-xs">
              {services.map((s) => (
                <option key={s.service} value={s.service}>
                  {s.service}
                  {s.service === origin ? " (probable origin)" : ""}
                </option>
              ))}
            </Select>
          </div>
        ) : (
          <span className="text-sm text-foreground-secondary">{selected.service}</span>
        )}
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <AnalysisStatusPill status={selected.status} size="sm" />
          {selected.status !== "INSUFFICIENT_DATA" ? (
            <SeverityBadge severity={selected.severity?.band ?? null} score={selected.severity?.score} size="sm" />
          ) : null}
        </div>
      </div>

      {!gate.passed ? (
        <p className="border-b px-5 py-3 text-sm text-foreground-secondary">
          Insufficient data: {formatCount(gate.baseline_requests)} requests before and {formatCount(gate.post_requests)} after,
          below the minimum of {formatCount(gate.min_requests)} per window. No other gate was evaluated.
        </p>
      ) : null}

      {/* Scrolls sideways on narrow screens, so it must be reachable by keyboard (WCAG 2.1.1). */}
      <div className="overflow-x-auto rounded-sm" tabIndex={0} role="region" aria-label={`${selected.service}: before and after metrics`}>
        <table className="w-full min-w-[40rem] text-sm">
          <caption className="sr-only">
            {selected.service}: metrics before and after the deployment, with the change, its interval and sample sizes
          </caption>
          <thead>
            <tr className="border-b text-xs text-muted-foreground">
              <th scope="col" className="px-5 py-2 text-left font-medium">Metric</th>
              <th scope="col" className="px-3 py-2 text-right font-medium">Before</th>
              <th scope="col" className="px-3 py-2 text-right font-medium">After</th>
              <th scope="col" className="px-3 py-2 text-right font-medium">Change</th>
              <th scope="col" className="px-3 py-2 text-left font-medium">Interval or test</th>
              <th scope="col" className="px-3 py-2 text-left font-medium">Samples</th>
              <th scope="col" className="px-5 py-2 text-left font-medium">Gates</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const Icon = DIRECTION_ICON[row.direction];
              return (
                <tr key={row.key} className="border-b last:border-b-0" data-row={row.key}>
                  <th scope="row" className="px-5 py-2.5 text-left font-medium whitespace-nowrap text-foreground">
                    {row.metric}
                  </th>
                  <td className="px-3 py-2.5 text-right whitespace-nowrap tabular-nums text-foreground-secondary">{row.before}</td>
                  <td className="px-3 py-2.5 text-right whitespace-nowrap tabular-nums text-foreground">{row.after}</td>
                  <td className="px-3 py-2.5 text-right whitespace-nowrap tabular-nums text-foreground">
                    <span className="inline-flex items-center gap-1">
                      <Icon aria-hidden="true" className="size-3.5 text-muted-foreground" />
                      <span className="sr-only">{DIRECTION_WORD[row.direction]}: </span>
                      {row.delta}
                    </span>
                  </td>
                  <td className="min-w-[11rem] px-3 py-2.5 text-xs text-foreground-secondary tabular-nums">{row.interval}</td>
                  <td className="px-3 py-2.5 text-xs whitespace-nowrap text-muted-foreground tabular-nums">{row.samples}</td>
                  <td className="px-5 py-2.5 text-xs whitespace-nowrap">
                    {row.interval === "Not tested" ? (
                      <span className="text-muted-foreground">Not tested</span>
                    ) : row.warning ? (
                      <span className="inline-flex items-center gap-1 font-medium text-warning">
                        <TriangleAlert aria-hidden="true" className="size-3.5" />
                        Dropped: warning
                      </span>
                    ) : row.regressed ? (
                      <span className="inline-flex items-center gap-1 font-medium text-critical">
                        <OctagonAlert aria-hidden="true" className="size-3.5" />
                        All passed: regressed
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1 text-foreground-secondary">
                        <CircleCheck aria-hidden="true" className="size-3.5" />
                        Within gates
                      </span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {notes.length ? (
        <ul className="space-y-0.5 border-t px-5 py-3 text-[11px] leading-5 text-muted-foreground">
          {notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

"use client";

import { LineChart as LineChartIcon } from "lucide-react";
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import type { LatencySeries } from "@/lib/analysis";
import { formatCount, formatMs, formatTime } from "@/lib/format";

const AXIS_TICK = { fill: "var(--text-muted)", fontSize: 11 };

function TooltipBody({ active, payload }: { active?: boolean; payload?: Array<{ payload: LatencySeries["points"][number] }> }) {
  const point = active ? payload?.[0]?.payload : undefined;
  if (!point) return null;
  return (
    <div className="rounded-md border bg-raised px-2.5 py-1.5 text-xs shadow-raised">
      <p className="font-medium text-foreground">{formatTime(new Date(point.t))}</p>
      <p className="text-foreground-secondary">p95 {formatMs(point.p95)}</p>
      <p className="text-muted-foreground">{formatCount(point.samples)} requests</p>
      {point.exceeds ? <p className="font-medium text-critical">Above the baseline&apos;s CI</p> : null}
    </div>
  );
}

/**
 * The per-minute p95 latency after the deploy, against the baseline p95 and the upper bound of
 * its confidence interval. These buckets are what the engine used to estimate the onset; it
 * stores no other latency time series, so nothing else is plotted.
 */
export function LatencyChart({ series }: { series: LatencySeries | null }) {
  return (
    <section aria-labelledby="latency-title" className="rounded-xl border bg-panel shadow-panel">
      <div className="flex flex-wrap items-center gap-2 border-b px-5 py-3.5">
        <LineChartIcon aria-hidden="true" className="size-4 text-muted-foreground" />
        <h2 id="latency-title" className="text-sm font-semibold">
          p95 latency after the deploy{series ? `: ${series.service}` : ""}
        </h2>
      </div>
      {!series ? (
        <p className="px-5 py-4 text-sm text-muted-foreground">
          No latency series for this analysis. The engine records per-minute p95 only for a service whose p95 latency
          regression passed every gate; the before/after table has the window-level numbers.
        </p>
      ) : (
        <>
          <div
            className="h-64 px-2 pt-4"
            role="img"
            aria-label={`p95 latency per ${series.bucketSeconds / 60}-minute bucket for ${series.service}, against a baseline p95 of ${formatMs(series.baselineP95)}. The data table below has every point.`}
          >
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={series.points} margin={{ top: 22, right: 24, bottom: 4, left: 8 }}>
                <CartesianGrid stroke="var(--border)" vertical={false} />
                <XAxis
                  dataKey="t"
                  type="number"
                  scale="time"
                  domain={["dataMin", "dataMax"]}
                  tickFormatter={(t: number) => formatTime(new Date(t)).replace(" UTC", "").slice(0, 5)}
                  tick={AXIS_TICK}
                  stroke="var(--border)"
                />
                <YAxis tick={AXIS_TICK} stroke="var(--border)" width={56} tickFormatter={(v: number) => formatMs(v)} />
                <Tooltip content={<TooltipBody />} cursor={{ stroke: "var(--border-strong)", strokeWidth: 1 }} />
                <ReferenceLine
                  y={series.baselineP95}
                  stroke="var(--text-muted)"
                  strokeDasharray="4 4"
                  label={{ value: `Baseline p95 ${formatMs(series.baselineP95)}`, position: "insideBottomLeft", fill: "var(--text-muted)", fontSize: 11 }}
                />
                <ReferenceLine
                  y={series.baselineCiHigh}
                  stroke="var(--text-muted)"
                  strokeDasharray="1 3"
                  label={{ value: `${Math.round(series.ciConfidence * 100)}% CI upper bound`, position: "insideTopRight", fill: "var(--text-muted)", fontSize: 11 }}
                />
                {series.onsetAt !== null ? (
                  <ReferenceLine
                    x={series.onsetAt}
                    stroke="var(--critical-fg)"
                    strokeDasharray="2 2"
                    label={{ value: "Onset", position: "top", fill: "var(--critical-fg)", fontSize: 11 }}
                  />
                ) : null}
                <Line
                  type="monotone"
                  dataKey="p95"
                  stroke="var(--brand)"
                  strokeWidth={2}
                  connectNulls={false}
                  isAnimationActive={false}
                  dot={(props: { cx?: number; cy?: number; payload?: LatencySeries["points"][number]; index?: number }) => {
                    const { cx, cy, payload, index } = props;
                    if (cx === undefined || cy === undefined || !payload || payload.p95 === null) return <g key={index} />;
                    return payload.exceeds ? (
                      <rect key={index} x={cx - 4} y={cy - 4} width={8} height={8} fill="var(--critical-fg)" stroke="var(--panel)" strokeWidth={2} />
                    ) : (
                      <circle key={index} cx={cx} cy={cy} r={4} fill="var(--brand)" stroke="var(--panel)" strokeWidth={2} />
                    );
                  }}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
          <div className="flex flex-wrap gap-x-4 gap-y-1 px-5 pb-1 text-xs text-foreground-secondary">
            <span className="inline-flex items-center gap-1.5">
              <svg aria-hidden="true" width="10" height="10"><circle cx="5" cy="5" r="4" fill="var(--brand)" /></svg>
              Minute within the baseline&apos;s range
            </span>
            <span className="inline-flex items-center gap-1.5">
              <svg aria-hidden="true" width="10" height="10"><rect width="9" height="9" fill="var(--critical-fg)" /></svg>
              Minute above the CI upper bound
            </span>
            <span className="text-muted-foreground">
              Buckets with fewer than {series.minSamples} requests have no p95 (gaps).
            </span>
          </div>
          <details className="px-5 pt-2 pb-4 text-xs">
            <summary className="cursor-pointer text-muted-foreground hover:text-foreground">Show the data table</summary>
            <table className="mt-2 w-full max-w-md text-left">
              <caption className="sr-only">p95 latency per bucket</caption>
              <thead>
                <tr className="text-muted-foreground">
                  <th scope="col" className="py-1 font-medium">Minute</th>
                  <th scope="col" className="py-1 text-right font-medium">p95</th>
                  <th scope="col" className="py-1 text-right font-medium">Requests</th>
                  <th scope="col" className="py-1 pl-3 font-medium">Above CI</th>
                </tr>
              </thead>
              <tbody className="tabular-nums">
                {series.points.map((p) => (
                  <tr key={p.t} className="border-t">
                    <td className="py-1">{formatTime(new Date(p.t))}</td>
                    <td className="py-1 text-right">{formatMs(p.p95)}</td>
                    <td className="py-1 text-right">{formatCount(p.samples)}</td>
                    <td className="py-1 pl-3">{p.exceeds ? "Yes" : "No"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        </>
      )}
    </section>
  );
}

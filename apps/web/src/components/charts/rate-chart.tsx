"use client";

import { Bar, BarChart, CartesianGrid, ErrorBar, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import type { ChartSpec, RatePoint } from "@/lib/findings";
import { formatCount } from "@/lib/format";

const TICK = { fill: "var(--text-muted)", fontSize: 11 };

export function formatValue(unit: ChartSpec["unit"], v: number | null): string {
  if (v === null) return "—";
  return unit === "rate" ? `${(v * 100).toFixed(1)}%` : `${Math.round(v).toLocaleString("en-US")} s`;
}

/** "28.1% (95% CI 27.8–28.4%, n = 94,546)" */
export function pointText(unit: ChartSpec["unit"], p: RatePoint, intervalLabel = "CI"): string {
  const ci = p.low !== null && p.high !== null ? ` (${intervalLabel} ${formatValue(unit, p.low)}–${formatValue(unit, p.high)}` : " (";
  return `${formatValue(unit, p.value)}${ci}${p.low !== null ? ", " : ""}n = ${formatCount(p.n)})`;
}

interface Row extends RatePoint {
  err: [number, number];
}

function rows(points: RatePoint[]): Row[] {
  return points.map((p) => ({
    ...p,
    err: [p.value !== null && p.low !== null ? p.value - p.low : 0, p.value !== null && p.high !== null ? p.high - p.value : 0],
  }));
}

function TooltipBody({ active, payload, unit, intervalLabel }: { active?: boolean; payload?: Array<{ payload: Row }>; unit: ChartSpec["unit"]; intervalLabel: string }) {
  const p = active ? payload?.[0]?.payload : undefined;
  if (!p) return null;
  return (
    <div className="max-w-64 rounded-md border bg-raised px-2.5 py-1.5 text-xs shadow-raised">
      <p className="font-medium text-foreground">{p.label}</p>
      <p className="text-foreground-secondary">{formatValue(unit, p.value)}</p>
      {p.low !== null && p.high !== null ? (
        <p className="text-muted-foreground">
          {intervalLabel}: {formatValue(unit, p.low)}–{formatValue(unit, p.high)}
        </p>
      ) : null}
      <p className="text-muted-foreground">n = {formatCount(p.n)}</p>
      {p.flagged ? <p className="text-muted-foreground">Low volume: excluded from the test</p> : null}
    </div>
  );
}

/**
 * One measure per category with its interval as error bars (single series: one hue, no legend;
 * the title names it). A data table with every value and interval sits under the chart.
 */
export function RateChart({ spec }: { spec: ChartSpec }) {
  const data = rows(spec.points);
  const max = Math.max(...spec.points.map((p) => p.high ?? p.value ?? 0));
  const domainMax = spec.unit === "rate" ? Math.min(1, Math.ceil((max * 1.1) * 20) / 20 || 0.05) : Math.ceil(max * 1.05);
  const tickFormatter = (v: number) => (spec.unit === "rate" ? `${Math.round(v * 100)}%` : `${Math.round(v)} s`);
  // Long category names would collide under vertical bars: lay those out horizontally.
  const horizontal = spec.kind === "horizontal" || (spec.kind === "vertical" && spec.points.some((p) => p.label.length > 12));
  const height = horizontal ? Math.max(160, data.length * 34 + 40) : 240;
  const tooltip = <Tooltip content={<TooltipBody unit={spec.unit} intervalLabel={spec.intervalLabel} />} cursor={{ fill: "var(--subtle)" }} />;

  return (
    <figure className="min-w-0">
      <figcaption className="mb-2 text-xs font-medium text-foreground-secondary">{spec.title}</figcaption>
      <div style={{ height }} role="img" aria-label={`${spec.title}. Bars show the value with its ${spec.intervalLabel}; the table below lists every value.`}>
        <ResponsiveContainer width="100%" height="100%">
          {spec.kind === "line" ? (
            <LineChart data={data} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
              <CartesianGrid stroke="var(--border)" vertical={false} />
              <XAxis dataKey="label" tick={TICK} stroke="var(--border)" interval="preserveStartEnd" minTickGap={24} />
              <YAxis tick={TICK} stroke="var(--border)" domain={[0, domainMax]} tickFormatter={tickFormatter} width={44} />
              {tooltip}
              <Line
                dataKey="value"
                stroke="var(--brand)"
                strokeWidth={2}
                isAnimationActive={false}
                dot={(props: { cx?: number; cy?: number; payload?: Row; index?: number }) => {
                  const { cx, cy, payload, index } = props;
                  if (cx === undefined || cy === undefined || !payload || payload.value === null) return <g key={index} />;
                  return (
                    <circle
                      key={index}
                      cx={cx}
                      cy={cy}
                      r={payload.flagged ? 3.5 : 3}
                      fill={payload.flagged ? "var(--panel)" : "var(--brand)"}
                      stroke="var(--brand)"
                      strokeWidth={payload.flagged ? 1.5 : 0}
                    />
                  );
                }}
              >
                <ErrorBar dataKey="err" width={0} stroke="var(--text-muted)" strokeWidth={1} direction="y" />
              </Line>
            </LineChart>
          ) : (
            <BarChart data={data} layout={horizontal ? "vertical" : "horizontal"} margin={{ top: 8, right: 24, bottom: 4, left: 0 }} barCategoryGap="28%">
              <CartesianGrid stroke="var(--border)" horizontal={!horizontal} vertical={horizontal} />
              {horizontal ? (
                <>
                  <XAxis type="number" domain={[0, domainMax]} tickFormatter={tickFormatter} tick={TICK} stroke="var(--border)" />
                  <YAxis type="category" dataKey="label" tick={TICK} stroke="var(--border)" width={190} interval={0} />
                </>
              ) : (
                <>
                  <XAxis dataKey="label" tick={TICK} stroke="var(--border)" interval={0} />
                  <YAxis domain={[0, domainMax]} tickFormatter={tickFormatter} tick={TICK} stroke="var(--border)" width={48} />
                </>
              )}
              {tooltip}
              <Bar dataKey="value" fill="var(--brand)" fillOpacity={0.85} radius={horizontal ? [0, 4, 4, 0] : [4, 4, 0, 0]} isAnimationActive={false}>
                <ErrorBar dataKey="err" width={6} stroke="var(--text)" strokeWidth={1.25} direction={horizontal ? "x" : "y"} />
              </Bar>
            </BarChart>
          )}
        </ResponsiveContainer>
      </div>
      {spec.note ? <p className="mt-1 text-[11px] text-muted-foreground">{spec.note}</p> : null}
      <details className="mt-1 text-xs">
        <summary className="cursor-pointer text-muted-foreground hover:text-foreground">Show the data table</summary>
        <div className="mt-2 max-h-64 overflow-auto">
          <table className="w-full text-left">
            <caption className="sr-only">{spec.title}</caption>
            <thead>
              <tr className="text-muted-foreground">
                <th scope="col" className="py-1 pr-2 font-medium">Group</th>
                <th scope="col" className="py-1 pr-2 text-right font-medium">{spec.unit === "rate" ? "Failure rate" : "Median"}</th>
                <th scope="col" className="py-1 pr-2 text-right font-medium">{spec.intervalLabel}</th>
                <th scope="col" className="py-1 text-right font-medium">n</th>
              </tr>
            </thead>
            <tbody className="tabular-nums">
              {spec.points.map((p) => (
                <tr key={p.key} className="border-t">
                  <th scope="row" className="py-1 pr-2 font-normal">
                    {p.label}
                    {p.flagged ? " (low volume)" : ""}
                  </th>
                  <td className="py-1 pr-2 text-right">{formatValue(spec.unit, p.value)}</td>
                  <td className="py-1 pr-2 text-right">
                    {p.low !== null && p.high !== null ? `${formatValue(spec.unit, p.low)}–${formatValue(spec.unit, p.high)}` : "—"}
                  </td>
                  <td className="py-1 text-right">{formatCount(p.n)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  );
}

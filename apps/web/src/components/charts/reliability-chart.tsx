"use client";

import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { formatCount } from "@/lib/format";

const TICK = { fill: "var(--text-muted)", fontSize: 11 };
const pct = (v: number) => `${Math.round(v * 100)}%`;

type Point = { predicted: number; observed: number; n: number };

function TooltipBody({ active, payload }: { active?: boolean; payload?: Array<{ payload: Point }> }) {
  const p = active ? payload?.[0]?.payload : undefined;
  if (!p) return null;
  return (
    <div className="rounded-md border bg-raised px-2.5 py-1.5 text-xs shadow-raised">
      <p className="text-foreground">Predicted {(p.predicted * 100).toFixed(1)}%</p>
      <p className="text-foreground-secondary">Observed {(p.observed * 100).toFixed(1)}%</p>
      <p className="text-muted-foreground">n = {formatCount(p.n)} builds</p>
    </div>
  );
}

/** Calibration on the test set: mean predicted probability vs observed failure rate per bin. */
export function ReliabilityChart({ points }: { points: Point[] }) {
  return (
    <figure>
      <div className="h-64" role="img" aria-label="Reliability curve: observed failure rate against predicted probability, with the diagonal for perfect calibration. The table below lists every bin.">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={points} margin={{ top: 8, right: 16, bottom: 16, left: 0 }}>
            <CartesianGrid stroke="var(--border)" />
            <XAxis
              dataKey="predicted"
              type="number"
              domain={[0, 1]}
              ticks={[0, 0.25, 0.5, 0.75, 1]}
              tickFormatter={pct}
              tick={TICK}
              stroke="var(--border)"
              label={{ value: "Predicted probability", position: "insideBottom", offset: -8, fill: "var(--text-muted)", fontSize: 11 }}
            />
            <YAxis domain={[0, 1]} ticks={[0, 0.25, 0.5, 0.75, 1]} tickFormatter={pct} tick={TICK} stroke="var(--border)" width={44} />
            <ReferenceLine
              segment={[
                { x: 0, y: 0 },
                { x: 1, y: 1 },
              ]}
              stroke="var(--text-muted)"
              strokeDasharray="4 4"
              label={{ value: "Perfect calibration", position: "insideTopLeft", fill: "var(--text-muted)", fontSize: 11 }}
            />
            <Tooltip content={<TooltipBody />} cursor={{ stroke: "var(--border-strong)" }} />
            <Line dataKey="observed" stroke="var(--brand)" strokeWidth={2} dot={{ r: 4, fill: "var(--brand)", stroke: "var(--panel)", strokeWidth: 2 }} isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
      <details className="mt-1 text-xs">
        <summary className="cursor-pointer text-muted-foreground hover:text-foreground">Show the data table</summary>
        <table className="mt-2 w-full max-w-sm text-left tabular-nums">
          <caption className="sr-only">Reliability bins</caption>
          <thead>
            <tr className="text-muted-foreground">
              <th scope="col" className="py-1 font-medium">Predicted</th>
              <th scope="col" className="py-1 text-right font-medium">Observed</th>
              <th scope="col" className="py-1 text-right font-medium">n</th>
            </tr>
          </thead>
          <tbody>
            {points.map((p) => (
              <tr key={p.predicted} className="border-t">
                <td className="py-1">{(p.predicted * 100).toFixed(1)}%</td>
                <td className="py-1 text-right">{(p.observed * 100).toFixed(1)}%</td>
                <td className="py-1 text-right">{formatCount(p.n)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </figure>
  );
}

import { ArrowDownRight, ArrowUpRight } from "lucide-react";

import type { RiskExplanation } from "@/lib/api/types";
import { cn } from "@/lib/utils";

function formatValue(value: RiskExplanation["value"]): string {
  if (value === null || value === undefined) return "unknown";
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "number") return Number.isInteger(value) ? value.toLocaleString("en-US") : value.toFixed(2);
  return value;
}

/**
 * SHAP contributions as diverging bars around zero: factors that raised the score point right,
 * those that lowered it point left. Direction is also stated in words and by an arrow icon.
 * These explain the model's score, not the cause of a failure.
 */
export function ShapBars({
  increasing,
  reducing,
  limit = 5,
  className,
}: {
  increasing: RiskExplanation[];
  reducing: RiskExplanation[];
  limit?: number;
  className?: string;
}) {
  const rows = [
    ...increasing.slice(0, limit).map((e) => ({ ...e, sign: 1 as const })),
    ...reducing.slice(0, limit).map((e) => ({ ...e, sign: -1 as const })),
  ];
  const max = Math.max(1e-9, ...rows.map((r) => Math.abs(r.contribution)));
  if (rows.length === 0) {
    return <p className={cn("text-sm text-muted-foreground", className)}>The model reported no explanation for this score.</p>;
  }
  return (
    <ul className={cn("space-y-2.5", className)} aria-label="Factors behind the score (SHAP contributions)">
      {rows.map((r) => {
        const width = `${(Math.abs(r.contribution) / max) * 50}%`;
        const Icon = r.sign > 0 ? ArrowUpRight : ArrowDownRight;
        return (
          <li key={`${r.sign}-${r.feature}`} className="text-sm">
            <div className="flex items-baseline justify-between gap-3">
              <span className="inline-flex min-w-0 items-center gap-1.5 text-foreground">
                <Icon aria-hidden="true" className={cn("size-3.5 shrink-0", r.sign > 0 ? "text-high" : "text-healthy")} />
                <span className="truncate">{r.label}</span>
                <span className="shrink-0 text-xs text-muted-foreground">= {formatValue(r.value)}</span>
              </span>
              <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
                {r.sign > 0 ? "raised" : "lowered"} {r.contribution >= 0 ? "+" : "−"}
                {Math.abs(r.contribution).toFixed(2)}
              </span>
            </div>
            <div aria-hidden="true" className="relative mt-1 h-2 rounded-full bg-subtle">
              <span className="absolute inset-y-0 left-1/2 w-px bg-border-strong" />
              <span
                className={cn("absolute inset-y-0 rounded-full", r.sign > 0 ? "left-1/2 bg-high" : "right-1/2 bg-healthy")}
                style={{ width }}
              />
            </div>
            {r.text ? <p className="mt-1 text-xs text-muted-foreground">{r.text}</p> : null}
          </li>
        );
      })}
    </ul>
  );
}

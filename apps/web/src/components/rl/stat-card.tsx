import { ArrowDownRight, ArrowUpRight, Info, Minus, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import { Tooltip } from "@/components/ui/tooltip";
import { TONE_CLASSES, type Tone } from "@/lib/tones";
import { cn } from "@/lib/utils";

export interface StatDelta {
  /** Pre-formatted change, e.g. "+12%" or "−3 ms". */
  value: string;
  direction: "up" | "down" | "flat";
  /** Whether this change is good, bad or neither for this metric (drives tone, never alone). */
  sentiment?: "positive" | "negative" | "neutral";
  /** What the change is measured against, e.g. "vs previous 7 days". */
  label?: string;
}

const DELTA_ICON: Record<StatDelta["direction"], LucideIcon> = {
  up: ArrowUpRight,
  down: ArrowDownRight,
  flat: Minus,
};

const SENTIMENT_TONE: Record<NonNullable<StatDelta["sentiment"]>, Tone> = {
  positive: "healthy",
  negative: "critical",
  neutral: "unknown",
};

/**
 * One number with its label. When the value is missing it renders "—" plus an info button whose
 * tooltip says why (never a made-up zero). `sampleSize` shows how many observations back it.
 */
export function StatCard({
  label,
  value,
  unit,
  delta,
  sampleSize,
  sampleLabel = "samples",
  missingReason,
  icon: Icon,
  className,
}: {
  label: string;
  value?: ReactNode | null;
  unit?: string;
  delta?: StatDelta;
  sampleSize?: number | null;
  sampleLabel?: string;
  /** Shown in a tooltip when `value` is missing. Say what will make the number appear. */
  missingReason: string;
  icon?: LucideIcon;
  className?: string;
}) {
  const missing = value === null || value === undefined || value === "";
  const DeltaIcon = delta ? DELTA_ICON[delta.direction] : null;
  const deltaTone = delta ? SENTIMENT_TONE[delta.sentiment ?? "neutral"] : "unknown";

  return (
    <div
      data-slot="stat-card"
      data-missing={missing || undefined}
      className={cn("flex min-w-0 flex-col rounded-xl border bg-panel p-4 shadow-panel", className)}
    >
      <div className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
        {Icon ? <Icon aria-hidden="true" className="size-3.5" /> : null}
        <span className="line-clamp-2">{label}</span>
      </div>

      <div className="mt-3 flex items-baseline gap-1.5">
        {missing ? (
          <>
            <span aria-hidden="true" className="text-2xl font-semibold tracking-tight text-muted-foreground">
              —
            </span>
            <span className="sr-only">No data.</span>
            <Tooltip content={missingReason} side="top" align="start">
              <button
                type="button"
                aria-label={`Why is ${label} empty?`}
                className="inline-flex size-6 translate-y-0.5 items-center justify-center self-center rounded-md text-muted-foreground hover:bg-subtle hover:text-foreground"
              >
                <Info aria-hidden="true" className="size-3.5" />
              </button>
            </Tooltip>
          </>
        ) : (
          <>
            <span className="text-2xl font-semibold tracking-tight text-foreground tabular-nums">{value}</span>
            {unit ? <span className="text-sm text-muted-foreground">{unit}</span> : null}
          </>
        )}
      </div>

      {(delta && !missing) || (sampleSize !== null && sampleSize !== undefined && !missing) ? (
        <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
          {delta && DeltaIcon && !missing ? (
            <span className={cn("inline-flex items-center gap-0.5 font-medium", TONE_CLASSES[deltaTone].text)}>
              <DeltaIcon aria-hidden="true" className="size-3.5" />
              <span className="sr-only">
                {delta.direction === "up" ? "Up" : delta.direction === "down" ? "Down" : "Unchanged"}
              </span>
              {delta.value}
            </span>
          ) : null}
          {delta?.label && !missing ? <span className="text-muted-foreground">{delta.label}</span> : null}
          {sampleSize !== null && sampleSize !== undefined && !missing ? (
            <span className="text-muted-foreground tabular-nums">
              n = {sampleSize.toLocaleString("en-US")} {sampleLabel}
            </span>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

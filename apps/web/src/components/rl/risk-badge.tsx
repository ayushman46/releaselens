import { CircleDashed } from "lucide-react";

import { normalizeScore, RISK_BAND_META, TONE_CLASSES, type RiskBand } from "@/lib/tones";
import { cn } from "@/lib/utils";

export function riskBadgeMeta(band: RiskBand) {
  return RISK_BAND_META[band];
}

/**
 * Pre-deploy CI failure risk: a 0–100 score plus the band the API assigned. The icon (signal
 * bars) and the band name carry the meaning; colour only reinforces it. This is a score, not a
 * probability of an outage.
 */
export function RiskBadge({
  score,
  band,
  size = "md",
  className,
}: {
  score: number | null | undefined;
  band: RiskBand | null | undefined;
  size?: "sm" | "md";
  className?: string;
}) {
  const sizeClasses = size === "sm" ? "h-5 px-1.5 text-[11px] gap-1" : "h-6 px-2 text-xs gap-1.5";
  const iconSize = size === "sm" ? "size-3" : "size-3.5";

  if (score === null || score === undefined || !band) {
    return (
      <span
        data-slot="risk-badge"
        data-tone="unknown"
        className={cn(
          "inline-flex shrink-0 items-center rounded-full border font-medium whitespace-nowrap",
          sizeClasses,
          TONE_CLASSES.unknown.pill,
          className,
        )}
      >
        <CircleDashed aria-hidden="true" className={iconSize} />
        <span className="sr-only">CI failure risk: </span>
        Not scored
      </span>
    );
  }

  const { tone, label, icon: Icon } = RISK_BAND_META[band];
  const value = normalizeScore(score);
  return (
    <span
      data-slot="risk-badge"
      data-tone={tone}
      data-band={band}
      className={cn(
        "inline-flex shrink-0 items-center rounded-full border font-medium whitespace-nowrap",
        sizeClasses,
        TONE_CLASSES[tone].pill,
        className,
      )}
    >
      <Icon aria-hidden="true" className={iconSize} />
      <span className="sr-only">CI failure risk: </span>
      <span className="tabular-nums">{value}</span>
      <span className="sr-only"> out of 100,</span>
      <span aria-hidden="true" className="opacity-50">
        ·
      </span>
      <span>{label}</span>
    </span>
  );
}

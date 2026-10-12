import type { LucideIcon } from "lucide-react";

import { TONE_CLASSES, TONE_META, type Tone } from "@/lib/tones";
import { cn } from "@/lib/utils";

export interface StatusPillProps {
  tone: Tone;
  /**
   * Visible text. Required: colour is never the only signal. A blank label falls back to the
   * tone's default ("Healthy", "Critical", …) so the pill can never render as colour + icon only.
   */
  label: string;
  /** Overrides the tone's icon (it cannot be removed). */
  icon?: LucideIcon;
  size?: "sm" | "md";
  className?: string;
  /** Extra context for screen readers, read before the label (e.g. "Severity:"). */
  srPrefix?: string;
  title?: string;
}

export function StatusPill({
  tone,
  label,
  icon,
  size = "md",
  className,
  srPrefix,
  title,
}: StatusPillProps) {
  const meta = TONE_META[tone];
  const Icon = icon ?? meta.icon;
  const text = label.trim() || meta.label;
  return (
    <span
      data-slot="status-pill"
      data-tone={tone}
      title={title}
      className={cn(
        "inline-flex shrink-0 items-center gap-1.5 rounded-full border font-medium whitespace-nowrap",
        size === "sm" ? "h-5 px-1.5 text-[11px]" : "h-6 px-2 text-xs",
        TONE_CLASSES[tone].pill,
        className,
      )}
    >
      <Icon aria-hidden="true" className={cn("shrink-0", size === "sm" ? "size-3" : "size-3.5")} />
      {srPrefix ? <span className="sr-only">{srPrefix} </span> : null}
      <span>{text}</span>
    </span>
  );
}

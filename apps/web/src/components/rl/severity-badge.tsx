import { StatusPill } from "@/components/rl/status-pill";
import { normalizeScore, SEVERITY_META, type SeverityBand } from "@/lib/tones";

/**
 * Post-deploy regression severity (v1): a deterministic score with a band, not a probability.
 * `null` means the gates could not decide (too few requests), shown as "Insufficient data".
 */
export function SeverityBadge({
  severity,
  score,
  size,
  className,
}: {
  severity: SeverityBand | null | undefined;
  score?: number | null;
  size?: "sm" | "md";
  className?: string;
}) {
  if (!severity) {
    return (
      <StatusPill
        tone="unknown"
        label="Insufficient data"
        srPrefix="Severity:"
        size={size}
        className={className}
      />
    );
  }
  const { tone, label } = SEVERITY_META[severity];
  const text = score === null || score === undefined ? label : `${label} · ${normalizeScore(score)}`;
  return <StatusPill tone={tone} label={text} srPrefix="Severity:" size={size} className={className} />;
}

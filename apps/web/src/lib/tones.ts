import {
  CircleAlert,
  CircleCheck,
  CircleDashed,
  Info,
  OctagonAlert,
  Signal,
  SignalHigh,
  SignalLow,
  SignalMedium,
  TriangleAlert,
  type LucideIcon,
} from "lucide-react";

/** The six semantic status families defined in globals.css. */
export type Tone = "healthy" | "warning" | "high" | "critical" | "unknown" | "info";

export const TONES: readonly Tone[] = ["healthy", "warning", "high", "critical", "unknown", "info"];

export interface ToneMeta {
  /** Default text label when a caller passes none (a pill never renders colour alone). */
  label: string;
  icon: LucideIcon;
}

export const TONE_META: Record<Tone, ToneMeta> = {
  healthy: { label: "Healthy", icon: CircleCheck },
  warning: { label: "Warning", icon: TriangleAlert },
  high: { label: "High", icon: CircleAlert },
  critical: { label: "Critical", icon: OctagonAlert },
  unknown: { label: "Insufficient data", icon: CircleDashed },
  info: { label: "Info", icon: Info },
};

/**
 * Literal class strings per tone, so Tailwind's scanner sees every utility.
 * `pill` = tinted fill + tone text; `text` = tone foreground only (icons, inline text).
 */
export const TONE_CLASSES: Record<Tone, { pill: string; text: string; surface: string }> = {
  healthy: {
    pill: "bg-healthy-bg text-healthy border-healthy-border",
    text: "text-healthy",
    surface: "bg-healthy-bg border-healthy-border",
  },
  warning: {
    pill: "bg-warning-bg text-warning border-warning-border",
    text: "text-warning",
    surface: "bg-warning-bg border-warning-border",
  },
  high: {
    pill: "bg-high-bg text-high border-high-border",
    text: "text-high",
    surface: "bg-high-bg border-high-border",
  },
  critical: {
    pill: "bg-critical-bg text-critical border-critical-border",
    text: "text-critical",
    surface: "bg-critical-bg border-critical-border",
  },
  unknown: {
    pill: "bg-unknown-bg text-unknown border-unknown-border",
    text: "text-unknown",
    surface: "bg-unknown-bg border-unknown-border",
  },
  info: {
    pill: "bg-info-bg text-info border-info-border",
    text: "text-info",
    surface: "bg-info-bg border-info-border",
  },
};

/* ------------------------------------------------------------------------------------------ */
/* Risk bands (pre-deploy CI failure risk). The band comes from the API; it is never derived  */
/* here, because the score-to-band cut-offs belong to the model (M4/M5).                      */
/* ------------------------------------------------------------------------------------------ */

export type RiskBand = "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";

export const RISK_BANDS: readonly RiskBand[] = ["LOW", "MEDIUM", "HIGH", "CRITICAL"];

export const RISK_BAND_META: Record<RiskBand, { tone: Tone; label: string; icon: LucideIcon }> = {
  LOW: { tone: "healthy", label: "Low", icon: SignalLow },
  MEDIUM: { tone: "warning", label: "Medium", icon: SignalMedium },
  HIGH: { tone: "high", label: "High", icon: SignalHigh },
  CRITICAL: { tone: "critical", label: "Critical", icon: Signal },
};

/* ------------------------------------------------------------------------------------------ */
/* Severity bands (post-deploy regression severity v1: a deterministic score, not a          */
/* probability).                                                                              */
/* ------------------------------------------------------------------------------------------ */

export type SeverityBand = "HEALTHY" | "WARNING" | "HIGH" | "CRITICAL";

export const SEVERITY_BANDS: readonly SeverityBand[] = ["HEALTHY", "WARNING", "HIGH", "CRITICAL"];

export const SEVERITY_META: Record<SeverityBand, { tone: Tone; label: string }> = {
  HEALTHY: { tone: "healthy", label: "Healthy" },
  WARNING: { tone: "warning", label: "Warning" },
  HIGH: { tone: "high", label: "High" },
  CRITICAL: { tone: "critical", label: "Critical" },
};

/** Scores are 0–100 integers on screen; anything else is clamped and rounded. */
export function normalizeScore(score: number): number {
  if (!Number.isFinite(score)) return 0;
  return Math.min(100, Math.max(0, Math.round(score)));
}

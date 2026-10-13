/** Formatting helpers. Every function returns "—" for a missing value: never a fake zero. */

export const DASH = "—";

function missing(value: number | null | undefined): value is null | undefined {
  return value === null || value === undefined || Number.isNaN(value);
}

/** 212 → "212 ms", 1534 → "1.53 s", 3.4 → "3.4 ms". */
export function formatMs(ms: number | null | undefined): string {
  if (missing(ms)) return DASH;
  if (ms === 0) return "0 ms";
  if (Math.abs(ms) >= 1000) return `${(ms / 1000).toFixed(2)} s`;
  if (Math.abs(ms) >= 100) return `${Math.round(ms)} ms`;
  if (Math.abs(ms) >= 10) return `${ms.toFixed(1)} ms`;
  return `${ms.toFixed(2)} ms`;
}

/** Signed milliseconds with a real minus sign: "+250 ms", "−12.5 ms". */
export function formatSignedMs(ms: number | null | undefined): string {
  if (missing(ms)) return DASH;
  const body = formatMs(Math.abs(ms));
  if (ms === 0) return body;
  return `${ms > 0 ? "+" : "−"}${body}`;
}

/** A proportion in [0, 1] as a percentage: 0.0351 → "3.51%". */
export function formatRate(rate: number | null | undefined, digits = 2): string {
  if (missing(rate)) return DASH;
  return `${(rate * 100).toFixed(digits)}%`;
}

/** Percentage points: 3.2 → "+3.20 pp". The input is already in points. */
export function formatPp(pp: number | null | undefined, digits = 2): string {
  if (missing(pp)) return DASH;
  const sign = pp > 0 ? "+" : pp < 0 ? "−" : "";
  return `${sign}${Math.abs(pp).toFixed(digits)} pp`;
}

/** A relative change: 0.183 → "+18.3%". */
export function formatRelative(rel: number | null | undefined, digits = 1): string {
  if (missing(rel)) return DASH;
  const sign = rel > 0 ? "+" : rel < 0 ? "−" : "";
  return `${sign}${Math.abs(rel * 100).toFixed(digits)}%`;
}

export function formatCount(n: number | null | undefined): string {
  if (missing(n)) return DASH;
  return Math.round(n).toLocaleString("en-US");
}

/** Requests per minute. */
export function formatRpm(rpm: number | null | undefined): string {
  if (missing(rpm)) return DASH;
  return `${rpm >= 100 ? Math.round(rpm).toLocaleString("en-US") : rpm.toFixed(1)} req/min`;
}

export function formatNumber(value: number | null | undefined, digits = 3): string {
  if (missing(value)) return DASH;
  return value.toFixed(digits);
}

const DATE_TIME = new Intl.DateTimeFormat("en-US", {
  year: "numeric",
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
  timeZone: "UTC",
  timeZoneName: "short",
});

const TIME = new Intl.DateTimeFormat("en-US", {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
  timeZone: "UTC",
});

const DATE = new Intl.DateTimeFormat("en-US", { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" });

function toDate(value: string | Date | null | undefined): Date | null {
  if (!value) return null;
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** Absolute time in UTC (the API's clock), e.g. "Oct 13, 2026, 02:10 UTC". */
export function formatDateTime(value: string | Date | null | undefined): string {
  const date = toDate(value);
  return date ? DATE_TIME.format(date) : DASH;
}

export function formatTime(value: string | Date | null | undefined): string {
  const date = toDate(value);
  return date ? `${TIME.format(date)} UTC` : DASH;
}

export function formatDate(value: string | Date | null | undefined): string {
  const date = toDate(value);
  return date ? DATE.format(date) : DASH;
}

/** "5 min ago", "3 h ago", "2 d ago"; future times say "in …". */
export function formatRelativeTime(value: string | Date | null | undefined, now: Date = new Date()): string {
  const date = toDate(value);
  if (!date) return DASH;
  const seconds = Math.round((now.getTime() - date.getTime()) / 1000);
  const abs = Math.abs(seconds);
  const unit =
    abs < 60 ? `${abs} s` : abs < 3600 ? `${Math.floor(abs / 60)} min` : abs < 86_400 ? `${Math.floor(abs / 3600)} h` : `${Math.floor(abs / 86_400)} d`;
  if (abs < 10) return "just now";
  return seconds >= 0 ? `${unit} ago` : `in ${unit}`;
}

/** Minutes between two instants, one decimal. */
export function minutesBetween(from: string | Date, to: string | Date): number | null {
  const a = toDate(from);
  const b = toDate(to);
  if (!a || !b) return null;
  return Math.round(((b.getTime() - a.getTime()) / 60_000) * 10) / 10;
}

export function shortSha(sha: string | null | undefined): string {
  if (!sha) return DASH;
  return sha.slice(0, 7);
}

/** 0.95 → "p95", 0.5 → "p50", 0.999 → "p99.9" (rl_core.regression.evidence.percentile_label). */
export function percentileLabel(q: number): string {
  return `p${Number((q * 100).toFixed(4))}`;
}

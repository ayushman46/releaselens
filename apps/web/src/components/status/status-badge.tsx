import {
  CircleCheck,
  CircleX,
  LoaderCircle,
  TriangleAlert,
  Unplug,
  type LucideIcon,
} from "lucide-react";

import type { HealthState } from "@/lib/status";
import { cn } from "@/lib/utils";

export type StatusTone = "success" | "warning" | "danger" | "neutral";

export interface StatusMeta {
  label: string;
  tone: StatusTone;
  icon: LucideIcon;
}

/** Colour is never the only signal: every state has its own icon and text label. */
export const STATUS_META: Record<HealthState, StatusMeta> = {
  ok: { label: "OK", tone: "success", icon: CircleCheck },
  ready: { label: "Ready", tone: "success", icon: CircleCheck },
  not_ready: { label: "Not ready", tone: "warning", icon: TriangleAlert },
  error: { label: "Error", tone: "danger", icon: CircleX },
  unreachable: { label: "Unreachable", tone: "danger", icon: Unplug },
  checking: { label: "Checking…", tone: "neutral", icon: LoaderCircle },
};

export function getStatusMeta(state: HealthState): StatusMeta {
  return STATUS_META[state];
}

const TONE_ICON_CLASS: Record<StatusTone, string> = {
  success: "text-success",
  warning: "text-warning",
  danger: "text-danger",
  neutral: "text-muted-foreground",
};

export function StatusIcon({ state, className }: { state: HealthState; className?: string }) {
  const { icon: Icon, tone } = getStatusMeta(state);
  return (
    <Icon
      aria-hidden="true"
      data-tone={tone}
      className={cn(
        "size-3.5 shrink-0",
        TONE_ICON_CLASS[tone],
        state === "checking" && "animate-spin motion-reduce:animate-none",
        className,
      )}
    />
  );
}

export function StatusBadge({
  state,
  label,
  className,
}: {
  state: HealthState;
  /** Overrides the default label for the state. */
  label?: string;
  className?: string;
}) {
  const meta = getStatusMeta(state);
  return (
    <span
      data-slot="status-badge"
      data-state={state}
      data-tone={meta.tone}
      className={cn(
        "inline-flex h-6 items-center gap-1.5 rounded-md border bg-background/60 px-2 text-xs font-medium whitespace-nowrap text-foreground",
        className,
      )}
    >
      <StatusIcon state={state} />
      {label ?? meta.label}
    </span>
  );
}

import { Circle, CircleCheck, CircleDashed, Crosshair, TriangleAlert, type LucideIcon } from "lucide-react";

import { RADIUS_ROLE_META, type RadiusRole } from "@/lib/analysis";
import { TONE_CLASSES } from "@/lib/tones";
import { cn } from "@/lib/utils";

export const RADIUS_ROLE_ICON: Record<RadiusRole, LucideIcon> = {
  origin: Crosshair,
  affected: TriangleAlert,
  healthy: CircleCheck,
  unknown: CircleDashed,
  outside: Circle,
};

export const BORDER_CLASS: Record<"solid" | "dashed" | "dotted" | "double", string> = {
  solid: "border-2 border-solid",
  dashed: "border-2 border-dashed",
  dotted: "border-2 border-dotted",
  double: "border-4 border-double",
};

/** Node chrome per role: border style + icon + text carry the meaning; colour reinforces it. */
export function radiusNodeClasses(role: RadiusRole): string {
  const meta = RADIUS_ROLE_META[role];
  return cn(
    BORDER_CLASS[meta.border],
    role === "outside" ? "border-border-strong/60 bg-panel" : TONE_CLASSES[meta.tone].surface,
  );
}

const ORDER: RadiusRole[] = ["origin", "affected", "healthy", "unknown", "outside"];

export function RadiusLegend({
  roles = ORDER,
  labels,
  className,
}: {
  roles?: RadiusRole[];
  /** Context-specific wording (e.g. the service map says "Not in the latest analysis"). */
  labels?: Partial<Record<RadiusRole, string>>;
  className?: string;
}) {
  return (
    <ul aria-label="Legend" className={cn("flex flex-wrap gap-x-4 gap-y-2 text-xs text-foreground-secondary", className)}>
      {roles.map((role) => {
        const Icon = RADIUS_ROLE_ICON[role];
        const meta = RADIUS_ROLE_META[role];
        return (
          <li key={role} className="inline-flex items-center gap-1.5" data-role={role}>
            <span aria-hidden="true" className={cn("inline-flex size-5 items-center justify-center rounded", radiusNodeClasses(role))}>
              <Icon className={cn("size-3", role === "outside" ? "text-muted-foreground" : TONE_CLASSES[meta.tone].text)} />
            </span>
            <span>{labels?.[role] ?? meta.label}</span>
            <span className="sr-only">: {meta.border} border</span>
          </li>
        );
      })}
    </ul>
  );
}

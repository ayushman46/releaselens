import {
  Activity,
  ArrowUp,
  ChevronRight,
  Gauge,
  Lightbulb,
  Repeat,
  Rocket,
  ScanSearch,
  type LucideIcon,
} from "lucide-react";

import { cn } from "@/lib/utils";

interface LoopStep {
  name: string;
  icon: LucideIcon;
  owner: "ReleaseLens" | "Your pipeline";
  description: string;
}

const STEPS: LoopStep[] = [
  {
    name: "Predict",
    icon: Gauge,
    owner: "ReleaseLens",
    description: "Scores the CI failure risk of a pull request from its change facts, and shows the factors behind the score.",
  },
  {
    name: "Deploy",
    icon: Rocket,
    owner: "Your pipeline",
    description: "Your CD pipeline ships as usual, then posts a deployment event. ReleaseLens never deploys code.",
  },
  {
    name: "Observe",
    icon: Activity,
    owner: "ReleaseLens",
    description: "Receives OpenTelemetry traces and builds per-service baselines before and after the release.",
  },
  {
    name: "Detect",
    icon: ScanSearch,
    owner: "ReleaseLens",
    description: "Flags latency and error regressions only when they clear statistical gates.",
  },
  {
    name: "Explain",
    icon: Lightbulb,
    owner: "ReleaseLens",
    description: "Ranks the probable origin in the service graph and maps the blast radius.",
  },
  {
    name: "Learn",
    icon: Repeat,
    owner: "ReleaseLens",
    description: "Joins each prediction with what actually happened, to evaluate and retrain the model.",
  },
];

/** Phase brackets over the six columns (large screens only; the cards carry the same meaning). */
const PHASES = [
  { label: "Before deploy", span: "col-span-1" },
  { label: "Deploy", span: "col-span-1" },
  { label: "After deploy", span: "col-span-3" },
  { label: "Over time", span: "col-span-1" },
];

/**
 * PREDICT → DEPLOY → OBSERVE → DETECT → EXPLAIN → LEARN, as an ordered list with decorative
 * connectors. Six columns with a return path on large screens, three on tablets, one on phones.
 */
export function LoopDiagram() {
  return (
    <div>
      <div aria-hidden="true" className="mb-3 hidden grid-cols-6 gap-6 lg:grid">
        {PHASES.map((phase) => (
          <div key={phase.label} className={cn(phase.span, "border-t border-border-strong/50 pt-2")}>
            <span className="text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
              {phase.label}
            </span>
          </div>
        ))}
      </div>

      <ol aria-label="The ReleaseLens loop" className="grid gap-4 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-6 lg:gap-6">
        {STEPS.map((step, index) => {
          const Icon = step.icon;
          const yours = step.owner === "Your pipeline";
          return (
            <li
              key={step.name}
              className={cn(
                "relative flex flex-col rounded-xl border p-4",
                yours ? "border-dashed bg-transparent" : "bg-panel shadow-panel",
              )}
            >
              <div className="flex items-center justify-between">
                <span
                  className={cn(
                    "flex size-8 items-center justify-center rounded-lg border",
                    yours ? "bg-transparent" : "bg-subtle",
                  )}
                >
                  <Icon aria-hidden="true" className={cn("size-4", yours ? "text-muted-foreground" : "text-brand-text")} />
                </span>
                <span className="font-mono text-[11px] text-muted-foreground tabular-nums">
                  {String(index + 1).padStart(2, "0")}
                </span>
              </div>
              <h3 className="mt-4 text-sm font-semibold tracking-wide uppercase">{step.name}</h3>
              <p className="mt-0.5 text-[11px] font-medium text-muted-foreground">{step.owner}</p>
              <p className="mt-2 text-[13px] leading-5 text-foreground-secondary">{step.description}</p>

              {index < STEPS.length - 1 ? (
                <ChevronRight
                  aria-hidden="true"
                  className="absolute top-1/2 -right-[19px] hidden size-3.5 -translate-y-1/2 text-muted-foreground lg:block"
                />
              ) : null}
            </li>
          );
        })}
      </ol>

      {/* Return path: LEARN feeds the next PREDICT. */}
      <div aria-hidden="true" className="relative mx-[8.333%] hidden h-10 lg:block">
        <div className="absolute inset-x-0 top-0 h-6 rounded-b-xl border-x border-b border-dashed border-border-strong/70" />
        <ArrowUp className="absolute -top-1 -left-[7px] size-3.5 text-muted-foreground" />
        <span className="absolute top-6 left-1/2 -translate-x-1/2 -translate-y-1/2 bg-background px-3 text-xs text-muted-foreground">
          Observed outcomes improve the next prediction
        </span>
      </div>
      <p className="mt-4 text-sm text-muted-foreground lg:hidden">
        The loop closes at Learn: observed outcomes improve the next prediction.
      </p>
    </div>
  );
}

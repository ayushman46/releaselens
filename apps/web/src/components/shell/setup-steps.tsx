import { ArrowRight, type LucideIcon } from "lucide-react";
import Link from "next/link";

import { StatusPill } from "@/components/rl/status-pill";
import { Badge } from "@/components/ui/badge";

export interface SetupStep {
  title: string;
  description: string;
  href: string;
  cta: string;
  icon: LucideIcon;
  /** Shown as a badge while the feature is still being built. */
  milestone?: string;
  /** True once data from this source has arrived. */
  connected?: boolean;
}

/** A connection checklist. A step reads "Connected" only once its data has actually arrived. */
export function SetupSteps({ steps, title }: { steps: SetupStep[]; title: string }) {
  return (
    <section aria-labelledby="setup-title" className="rounded-xl border bg-panel shadow-panel">
      <div className="flex items-center justify-between gap-3 border-b px-5 py-3.5">
        <h2 id="setup-title" className="text-sm font-semibold">
          {title}
        </h2>
        <span className="text-xs text-muted-foreground">
          {steps.filter((s) => s.connected).length} of {steps.length} connected
        </span>
      </div>
      <ol className="divide-y">
        {steps.map((step, index) => {
          const Icon = step.icon;
          return (
            <li key={step.title} className="flex flex-col gap-3 px-5 py-4 sm:flex-row sm:items-center sm:gap-4">
              <div className="flex min-w-0 flex-1 items-start gap-3.5">
                <span className="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-lg border bg-subtle">
                  <Icon aria-hidden="true" className="size-4 text-foreground-secondary" />
                </span>
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <h3 className="text-sm font-medium">
                      <span className="sr-only">Step {index + 1}: </span>
                      {step.title}
                    </h3>
                    {step.milestone ? <Badge>Available in {step.milestone}</Badge> : null}
                  </div>
                  <p className="mt-0.5 text-sm leading-6 text-muted-foreground">{step.description}</p>
                </div>
              </div>
              <div className="flex items-center gap-3 pl-11 sm:pl-0">
                <StatusPill tone={step.connected ? "healthy" : "unknown"} label={step.connected ? "Connected" : "Not connected"} size="sm" />
                <Link
                  href={step.href}
                  className="inline-flex items-center gap-1 rounded-sm text-sm font-medium text-brand-text hover:underline"
                >
                  {step.cta}
                  <ArrowRight aria-hidden="true" className="size-3.5" />
                </Link>
              </div>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

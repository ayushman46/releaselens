import { Check } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { PLANS, type Plan } from "@/lib/pricing";
import { cn } from "@/lib/utils";

function PlanCard({ plan, headingLevel }: { plan: Plan; headingLevel: 2 | 3 }) {
  const Heading = headingLevel === 2 ? "h2" : "h3";
  const lines = [
    ...(plan.includes ? [plan.includes] : []),
    plan.limits.projects,
    plan.limits.services,
    plan.limits.retention,
    ...plan.features,
  ];
  return (
    <div
      className={cn(
        "relative flex flex-col rounded-xl border bg-panel p-6 shadow-panel",
        plan.highlighted && "border-brand/60 ring-1 ring-brand/30",
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <Heading className="text-sm font-semibold">{plan.name}</Heading>
        {plan.highlighted ? <Badge variant="brand">For growing teams</Badge> : null}
      </div>
      <p className="mt-4 flex items-baseline gap-1">
        <span className="text-3xl font-semibold tracking-tight">{plan.price}</span>
        {plan.cadence ? <span className="text-sm text-muted-foreground">{plan.cadence}</span> : null}
      </p>
      <p className="mt-2 text-sm text-muted-foreground">{plan.tagline}</p>
      <ul className="mt-6 space-y-2.5 border-t pt-5 text-sm">
        {lines.map((line) => (
          <li key={line} className="flex items-start gap-2.5 text-foreground-secondary">
            <Check aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-brand-text" />
            {line}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function PricingCards({ headingLevel = 3 }: { headingLevel?: 2 | 3 }) {
  return (
    <div className="grid gap-4 md:grid-cols-3">
      {PLANS.map((plan) => (
        <PlanCard key={plan.id} plan={plan} headingLevel={headingLevel} />
      ))}
    </div>
  );
}

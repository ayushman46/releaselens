import { Activity, ArrowRight, Gauge, Lightbulb, Repeat, Rocket, ScanSearch, type LucideIcon } from "lucide-react";
import Link from "next/link";

import { Button } from "@/components/ui/button";

const LOOP: Array<{ step: string; icon: LucideIcon }> = [
  { step: "Predict", icon: Gauge },
  { step: "Deploy", icon: Rocket },
  { step: "Observe", icon: Activity },
  { step: "Detect", icon: ScanSearch },
  { step: "Explain", icon: Lightbulb },
  { step: "Learn", icon: Repeat },
];

export default function HomePage() {
  return (
    <div className="mx-auto max-w-5xl px-4 py-16 sm:px-6 sm:py-24">
      <p className="inline-flex items-center gap-2 rounded-full border px-2.5 py-0.5 text-xs text-muted-foreground">
        <span aria-hidden="true" className="size-1.5 rounded-full bg-muted-foreground" />
        In development
      </p>

      <h1 className="mt-6 max-w-3xl text-3xl font-semibold tracking-tight text-balance sm:text-5xl">
        ReleaseLens
        <span className="sr-only"> — </span>
        <span className="mt-2 block text-muted-foreground">
          Release intelligence for engineering teams
        </span>
      </h1>

      <p className="mt-6 max-w-2xl text-base leading-7 text-pretty text-muted-foreground">
        ReleaseLens closes the loop around every release. It{" "}
        <strong className="font-medium text-foreground">predicts</strong> the risk of a change in CI
        before it merges, records each{" "}
        <strong className="font-medium text-foreground">deploy</strong> your existing pipeline
        reports, <strong className="font-medium text-foreground">observes</strong> your services
        through OpenTelemetry traces,{" "}
        <strong className="font-medium text-foreground">detects</strong> latency and error
        regressions against the pre-deploy baseline,{" "}
        <strong className="font-medium text-foreground">explains</strong> them by pointing to the
        probable origin in your service graph, and{" "}
        <strong className="font-medium text-foreground">learns</strong> from what actually happened
        to sharpen the next prediction. It plugs into the GitHub and CI/CD setup you already have,
        and never deploys code itself.
      </p>

      <ol
        aria-label="The ReleaseLens loop"
        className="mt-10 flex flex-wrap items-center gap-x-1.5 gap-y-2 text-xs"
      >
        {LOOP.map(({ step, icon: Icon }, index) => (
          <li key={step} className="flex items-center gap-1.5">
            <span className="inline-flex h-7 items-center gap-1.5 rounded-md border bg-card px-2.5 font-medium tracking-wide uppercase">
              <Icon aria-hidden="true" className="size-3.5 text-muted-foreground" />
              {step}
            </span>
            {index < LOOP.length - 1 ? (
              <ArrowRight aria-hidden="true" className="size-3.5 text-muted-foreground/60" />
            ) : null}
          </li>
        ))}
      </ol>

      <div className="mt-12 flex flex-wrap items-center gap-3">
        <Button asChild>
          <Link href="/status">
            View system status
            <ArrowRight aria-hidden="true" />
          </Link>
        </Button>
      </div>
    </div>
  );
}

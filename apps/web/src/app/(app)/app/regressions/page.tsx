import { Activity, Radio, Rocket, ScanSearch } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";

import { StatusPill } from "@/components/rl/status-pill";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { KeyValueList } from "@/components/ui/key-value";
import { PageHeader } from "@/components/ui/page-header";

export const metadata: Metadata = { title: "Regressions" };

const REQUIREMENTS = [
  { label: "Deployment events", icon: Rocket, href: "/app/deployments" },
  { label: "OpenTelemetry traces", icon: Radio, href: "/app/services" },
];

export default function RegressionsPage() {
  return (
    <>
      <PageHeader
        title="Regressions"
        description="Latency and error changes after each deployment, the probable origin and the services affected."
        breadcrumbs={[{ label: "Dashboard", href: "/app" }, { label: "Regressions" }]}
      />

      <div className="grid gap-6 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <EmptyState
          icon={ScanSearch}
          title="No regressions analysed yet"
          description="After each deployment, ReleaseLens compares every affected service before and after the release. A regression is reported only when it clears the statistical gates. With too little traffic you see “Insufficient data”, not a guess."
          action={
            <Button asChild size="sm">
              <Link href="/app/deployments">Send a deployment event</Link>
            </Button>
          }
          secondaryAction={
            <Button asChild size="sm" variant="ghost">
              <Link href="/docs/methodology">How detection works</Link>
            </Button>
          }
        >
          <div className="rounded-lg border bg-panel">
            <p className="border-b px-4 py-2.5 text-xs font-medium text-muted-foreground">Needs</p>
            <ul className="divide-y">
              {REQUIREMENTS.map((req) => {
                const Icon = req.icon;
                return (
                  <li key={req.label} className="flex items-center justify-between gap-3 px-4 py-3 text-sm">
                    <Link href={req.href} className="inline-flex items-center gap-2.5 rounded-sm font-medium hover:underline">
                      <Icon aria-hidden="true" className="size-4 text-muted-foreground" />
                      {req.label}
                    </Link>
                    <StatusPill tone="unknown" label="Not connected" size="sm" />
                  </li>
                );
              })}
            </ul>
          </div>
        </EmptyState>

        <section aria-labelledby="engine-title" className="self-start rounded-xl border bg-panel shadow-panel">
          <div className="flex items-center gap-2 border-b px-5 py-3.5">
            <Activity aria-hidden="true" className="size-4 text-muted-foreground" />
            <h2 id="engine-title" className="text-sm font-semibold">
              Detection design (engine v1, M8)
            </h2>
          </div>
          <div className="px-5">
            <KeyValueList
              labelWidth="sm:grid-cols-[minmax(0,7.5rem)_minmax(0,1fr)]"
              items={[
                { label: "Baseline", value: "The 30 minutes before the deployment" },
                { label: "Comparison", value: "Minutes 2 to 15 after the deployment" },
                {
                  label: "Latency gate",
                  value: "At least 25% and 20 ms slower, with a bootstrap confidence interval that excludes zero",
                },
                {
                  label: "Error gate",
                  value: "Two-proportion z-test p < 0.01, at least +0.5 percentage points and +50% relative",
                },
                { label: "Minimum traffic", value: "Below the per-service request minimum, the verdict is “Insufficient data”" },
                { label: "Output", value: "Probable origin, blast radius and a severity score (not a probability)" },
              ]}
            />
          </div>
        </section>
      </div>
    </>
  );
}

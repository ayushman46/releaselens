import { Activity, Boxes, Gauge, GitPullRequest, Inbox, Radio, Rocket } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";

import { StatCard } from "@/components/rl/stat-card";
import { SetupSteps } from "@/components/shell/setup-steps";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { PageHeader } from "@/components/ui/page-header";

export const metadata: Metadata = { title: "Overview" };

export default function OverviewPage() {
  return (
    <>
      <PageHeader
        title="Overview"
        description="Release health across your projects: CI failure risk before deploy, regressions after it."
        actions={
          <Button asChild variant="outline" size="sm">
            <Link href="/docs/quick-start">Quick start guide</Link>
          </Button>
        }
      />

      <section aria-label="Last 7 days" className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label="Deployments · 7 days"
          icon={Rocket}
          missingReason="No deployment events yet. The count starts with the first POST /api/v1/deployments from your pipeline."
        />
        <StatCard
          label="Regressions · 7 days"
          icon={Activity}
          missingReason="Regressions are found by comparing traces before and after each deployment, so this needs deployment events and OTLP traces."
        />
        <StatCard
          label="High-risk changes · 7 days"
          icon={Gauge}
          missingReason="Risk scores appear when the ReleaseLens GitHub Action runs on your pull requests."
        />
        <StatCard
          label="Services reporting"
          icon={Boxes}
          missingReason="Services appear when spans arrive at the OTLP endpoint."
        />
      </section>

      <div className="mt-6 grid gap-6 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <SetupSteps
          title="Connect ReleaseLens"
          steps={[
            {
              title: "Score pull requests",
              description: "Add the GitHub Action. It computes change facts in your runner and requests a CI failure risk score.",
              href: "/app/risk",
              cta: "Set up",
              icon: GitPullRequest,
              milestone: "M5",
            },
            {
              title: "Report deployments",
              description: "Post one event per release from your CD pipeline. This starts the before/after comparison.",
              href: "/app/deployments",
              cta: "Set up",
              icon: Rocket,
              milestone: "M6",
            },
            {
              title: "Send traces",
              description: "Point your OpenTelemetry Collector at the OTLP endpoint. Services and the service map come from these traces.",
              href: "/app/services",
              cta: "Set up",
              icon: Radio,
              milestone: "M7",
            },
          ]}
        />
        <EmptyState
          icon={Inbox}
          title="No activity yet"
          description="Deployments, risk checks and detected regressions will be listed here, newest first, as soon as the first one arrives."
          className="h-full justify-center"
        />
      </div>
    </>
  );
}

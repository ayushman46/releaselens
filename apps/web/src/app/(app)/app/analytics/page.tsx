import { ChartColumn } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { PageHeader } from "@/components/ui/page-header";

export const metadata: Metadata = { title: "Analytics" };

export default function AnalyticsPage() {
  return (
    <>
      <PageHeader
        title="Analytics"
        description="Trends in your delivery data, plus findings from the historical CI study the model is trained on."
        breadcrumbs={[{ label: "Dashboard", href: "/app" }, { label: "Analytics" }]}
      />
      <EmptyState
        icon={ChartColumn}
        title="Nothing to analyse yet"
        description="Charts appear once deployments, risk checks and regressions accumulate. Every chart will show its sample size, and every finding from the historical study its effect size and confidence interval, so a trend is never shown without the evidence behind it."
        action={
          <Button asChild size="sm">
            <Link href="/app/deployments">Send deployment events</Link>
          </Button>
        }
        secondaryAction={
          <Button asChild size="sm" variant="ghost">
            <Link href="/docs/methodology">Methodology</Link>
          </Button>
        }
      />
    </>
  );
}

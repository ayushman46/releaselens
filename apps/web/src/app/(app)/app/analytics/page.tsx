import type { Metadata } from "next";

import { AnalyticsView } from "@/components/analytics/analytics-view";
import { PageHeader } from "@/components/ui/page-header";
import findings from "@/data/findings.json";
import { buildReportView } from "@/lib/findings";

export const metadata: Metadata = { title: "Analytics" };

// Global reference data from the historical CI study (docs/analytics/findings.json, copied by
// `pnpm sync:data`). Trimmed on the server: only the view model is sent to the browser.
const report = buildReportView(findings as unknown as Record<string, unknown>);

export default function AnalyticsPage() {
  return (
    <>
      <PageHeader
        title="Analytics"
        description="Findings from the historical CI study the risk model is trained on, each with its effect size, confidence interval and caveats."
        breadcrumbs={[{ label: "Dashboard", href: "/app" }, { label: "Analytics" }]}
      />
      <AnalyticsView report={report} />
    </>
  );
}

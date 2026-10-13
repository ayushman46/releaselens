import { BookOpen } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";
import { Suspense } from "react";

import { RiskListView, RiskMeaning } from "@/components/risk/risk-views";
import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/ui/page-header";
import { LoadingState } from "@/components/ui/skeleton";

export const metadata: Metadata = { title: "Risk analysis" };

export default function RiskAnalysisPage() {
  return (
    <>
      <PageHeader
        title="Risk analysis"
        description="CI failure risk for each pull request, scored before merge, with the factors behind every score."
        breadcrumbs={[{ label: "Dashboard", href: "/app" }, { label: "Risk analysis" }]}
        actions={
          <Button asChild variant="outline" size="sm">
            <Link href="/docs/github-setup">
              <BookOpen aria-hidden="true" />
              GitHub setup
            </Link>
          </Button>
        }
      />
      <RiskMeaning />
      <Suspense fallback={<LoadingState label="Loading risk assessments…" lines={6} />}>
        <RiskListView />
      </Suspense>
    </>
  );
}

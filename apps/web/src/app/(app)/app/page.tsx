import type { Metadata } from "next";
import Link from "next/link";

import { OverviewView } from "@/components/overview/overview-view";
import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/ui/page-header";

export const metadata: Metadata = { title: "Overview" };

export default function OverviewPage() {
  return (
    <>
      <PageHeader
        title="Overview"
        description="Release health across your organization: CI failure risk before deploy, regressions after it."
        actions={
          <Button asChild variant="outline" size="sm">
            <Link href="/docs/quick-start">Quick start guide</Link>
          </Button>
        }
      />
      <OverviewView />
    </>
  );
}

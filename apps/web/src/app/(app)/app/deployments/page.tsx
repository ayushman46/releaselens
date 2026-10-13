import { BookOpen } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";
import { Suspense } from "react";

import { DeploymentsView } from "@/components/deployments/deployments-view";
import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/ui/page-header";
import { LoadingState } from "@/components/ui/skeleton";

export const metadata: Metadata = { title: "Deployments" };

export default function DeploymentsPage() {
  return (
    <>
      <PageHeader
        title="Deployments"
        description="Every release your CD pipeline reports, with the regression check that followed. Select one to see the evidence."
        breadcrumbs={[{ label: "Dashboard", href: "/app" }, { label: "Deployments" }]}
        actions={
          <Button asChild variant="outline" size="sm">
            <Link href="/docs/deployment-api">
              <BookOpen aria-hidden="true" />
              Deployment API
            </Link>
          </Button>
        }
      />
      <Suspense fallback={<LoadingState label="Loading deployments…" lines={6} />}>
        <DeploymentsView />
      </Suspense>
    </>
  );
}

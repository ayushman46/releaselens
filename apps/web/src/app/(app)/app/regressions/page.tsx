import { BookOpen } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";
import { Suspense } from "react";

import { RegressionsView } from "@/components/regressions/regressions-view";
import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/ui/page-header";
import { LoadingState } from "@/components/ui/skeleton";

export const metadata: Metadata = { title: "Regressions" };

export default function RegressionsPage() {
  return (
    <>
      <PageHeader
        title="Regressions"
        description="Deployments whose latest analysis found a regression or a warning, with the probable origin and the services affected."
        breadcrumbs={[{ label: "Dashboard", href: "/app" }, { label: "Regressions" }]}
        actions={
          <Button asChild variant="outline" size="sm">
            <Link href="/docs/methodology">
              <BookOpen aria-hidden="true" />
              How detection works
            </Link>
          </Button>
        }
      />
      <Suspense fallback={<LoadingState label="Loading regressions…" lines={6} />}>
        <RegressionsView />
      </Suspense>
    </>
  );
}

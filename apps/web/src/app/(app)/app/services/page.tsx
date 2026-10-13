import { BookOpen, Network } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";
import { Suspense } from "react";

import { ServicesView } from "@/components/services/services-view";
import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/ui/page-header";
import { LoadingState } from "@/components/ui/skeleton";

export const metadata: Metadata = { title: "Services" };

export default function ServicesPage() {
  return (
    <>
      <PageHeader
        title="Services"
        description="Every service that sends traces, with its traffic, error rate, latency and last deployment."
        breadcrumbs={[{ label: "Dashboard", href: "/app" }, { label: "Services" }]}
        actions={
          <>
            <Button asChild variant="outline" size="sm">
              <Link href="/app/service-map">
                <Network aria-hidden="true" />
                Service map
              </Link>
            </Button>
            <Button asChild variant="outline" size="sm">
              <Link href="/docs/otel-setup">
                <BookOpen aria-hidden="true" />
                OpenTelemetry setup
              </Link>
            </Button>
          </>
        }
      />
      <Suspense fallback={<LoadingState label="Loading services…" lines={6} />}>
        <ServicesView />
      </Suspense>
    </>
  );
}

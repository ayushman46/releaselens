import { Network } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { PageHeader } from "@/components/ui/page-header";

export const metadata: Metadata = { title: "Service map" };

export default function ServiceMapPage() {
  return (
    <>
      <PageHeader
        title="Service map"
        description="How your services call each other, built from real traces and used to place a regression's probable origin and blast radius."
        breadcrumbs={[{ label: "Dashboard", href: "/app" }, { label: "Service map" }]}
      />
      <EmptyState
        icon={Network}
        title="No service graph yet"
        description="The map is drawn only from traces: every parent-to-child span that crosses a service boundary becomes an edge. Nothing is configured by hand or guessed from deployment manifests, so the map appears once traces arrive."
        action={
          <Button asChild size="sm">
            <Link href="/app/services">Send traces</Link>
          </Button>
        }
        secondaryAction={
          <Button asChild size="sm" variant="ghost">
            <Link href="/docs/methodology">How origins are ranked</Link>
          </Button>
        }
      />
    </>
  );
}

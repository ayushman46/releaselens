import type { Metadata } from "next";

import { ServiceMapLoader } from "@/components/services/service-map-loader";
import { PageHeader } from "@/components/ui/page-header";

export const metadata: Metadata = { title: "Service map" };

export default function ServiceMapPage() {
  return (
    <>
      <PageHeader
        title="Service map"
        description="How your services call each other, built from real traces, with each service's health from the latest analysis. Select a service for details."
        breadcrumbs={[{ label: "Dashboard", href: "/app" }, { label: "Service map" }]}
      />
      <ServiceMapLoader />
    </>
  );
}

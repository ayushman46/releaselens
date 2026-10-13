"use client";

import dynamic from "next/dynamic";

import { LoadingState } from "@/components/ui/skeleton";

// React Flow measures the DOM; render the map on the client only.
export const ServiceMapLoader = dynamic(() => import("@/components/services/service-map-view").then((m) => m.ServiceMapView), {
  ssr: false,
  loading: () => <LoadingState label="Loading the service map…" lines={6} />,
});

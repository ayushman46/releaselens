"use client";

import dynamic from "next/dynamic";

import { Skeleton } from "@/components/ui/skeleton";

export const ReliabilityLoader = dynamic(() => import("@/components/charts/reliability-chart").then((m) => m.ReliabilityChart), {
  ssr: false,
  loading: () => <Skeleton className="h-64 rounded-lg" />,
});

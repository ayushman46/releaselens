"use client";

import { useQuery } from "@tanstack/react-query";

import { fetchLiveness, fetchReadiness } from "@/lib/api/health";
import { API_BASE_URL } from "@/lib/env";

export const HEALTH_REFETCH_INTERVAL_MS = 15_000;

export const healthQueryKeys = {
  all: ["api-health"] as const,
  liveness: (baseUrl: string) => ["api-health", baseUrl, "liveness"] as const,
  readiness: (baseUrl: string) => ["api-health", baseUrl, "readiness"] as const,
};

export function useApiHealth(baseUrl: string = API_BASE_URL) {
  const liveness = useQuery({
    queryKey: healthQueryKeys.liveness(baseUrl),
    queryFn: ({ signal }) => fetchLiveness(baseUrl, { signal }),
    refetchInterval: HEALTH_REFETCH_INTERVAL_MS,
    // A status page should show failures immediately; polling and the Retry button re-check.
    retry: false,
    staleTime: 0,
  });

  const readiness = useQuery({
    queryKey: healthQueryKeys.readiness(baseUrl),
    queryFn: ({ signal }) => fetchReadiness(baseUrl, { signal }),
    refetchInterval: HEALTH_REFETCH_INTERVAL_MS,
    retry: false,
    staleTime: 0,
  });

  return { liveness, readiness };
}

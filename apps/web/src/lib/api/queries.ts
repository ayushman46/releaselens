"use client";

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import { api, unwrap } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { riskBand } from "@/lib/api/types";

/**
 * TanStack Query hooks over the typed client. Tenant data keys always start with the org id, so
 * switching organizations never shows the previous org's cached rows.
 */

const PAGE_SIZE = 50;

/** Don't retry what retrying cannot fix (auth, permissions, missing, validation). */
export function shouldRetry(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.kind === "http" && error.status >= 400 && error.status < 500) return false;
  return failureCount < 1;
}

export function useMe() {
  return useQuery({
    queryKey: ["me"],
    queryFn: ({ signal }) => unwrap(api.GET("/api/v1/me", { signal })),
    retry: shouldRetry,
    staleTime: 60_000,
  });
}

export function usePlans() {
  return useQuery({
    queryKey: ["plans"],
    queryFn: ({ signal }) => unwrap(api.GET("/api/v1/plans", { signal })),
    staleTime: 10 * 60_000,
    retry: shouldRetry,
  });
}

export function useOrg(orgId: string | undefined) {
  return useQuery({
    queryKey: [orgId, "org"],
    enabled: Boolean(orgId),
    queryFn: ({ signal }) => unwrap(api.GET("/api/v1/orgs/{org_id}", { params: { path: { org_id: orgId! } }, signal })),
    retry: shouldRetry,
  });
}

export interface DeploymentFilters {
  service?: string;
  environment?: string;
}

function clean<T extends Record<string, string | undefined>>(filters: T): T {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(filters)) if (v && v.trim()) out[k] = v.trim();
  return out as T;
}

export function useDeployments(orgId: string | undefined, filters: DeploymentFilters) {
  const query = clean({ service: filters.service, environment: filters.environment });
  return useInfiniteQuery({
    queryKey: [orgId, "deployments", query],
    enabled: Boolean(orgId),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      unwrap(
        api.GET("/api/v1/deployments", {
          params: { query: { ...query, limit: PAGE_SIZE, cursor: pageParam } },
          signal,
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    retry: shouldRetry,
  });
}

export function useDeployment(orgId: string | undefined, deploymentId: string) {
  return useQuery({
    queryKey: [orgId, "deployment", deploymentId],
    enabled: Boolean(orgId && deploymentId),
    queryFn: ({ signal }) =>
      unwrap(api.GET("/api/v1/deployments/{deployment_id}", { params: { path: { deployment_id: deploymentId } }, signal })),
    retry: shouldRetry,
  });
}

export function useDeploymentAnalysis(orgId: string | undefined, deploymentId: string) {
  return useQuery({
    queryKey: [orgId, "deployment", deploymentId, "analysis"],
    enabled: Boolean(orgId && deploymentId),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/api/v1/deployments/{deployment_id}/analysis", {
          params: { path: { deployment_id: deploymentId } },
          signal,
        }),
      ),
    retry: shouldRetry,
    // Early analyses (T+5, T+10) are superseded by the final one: poll until it exists.
    refetchInterval: (q) => {
      const data = q.state.data;
      if (!data) return false;
      return data.analyses.some((a) => a.kind === "final") ? false : 60_000;
    },
  });
}

export function useRegressions(orgId: string | undefined, filters: DeploymentFilters) {
  const query = clean({ service: filters.service, environment: filters.environment });
  return useInfiniteQuery({
    queryKey: [orgId, "regressions", query],
    enabled: Boolean(orgId),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      unwrap(api.GET("/api/v1/regressions", { params: { query: { ...query, limit: PAGE_SIZE, cursor: pageParam } }, signal })),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    retry: shouldRetry,
  });
}

export function useServices(orgId: string | undefined, environment?: string) {
  const query = clean({ environment });
  return useInfiniteQuery({
    queryKey: [orgId, "services", query],
    enabled: Boolean(orgId),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      unwrap(api.GET("/api/v1/services", { params: { query: { ...query, limit: 200, cursor: pageParam } }, signal })),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    retry: shouldRetry,
    refetchInterval: 60_000,
  });
}

export function useServiceMap(orgId: string | undefined, environment: string | undefined, windowHours = 24) {
  return useQuery({
    queryKey: [orgId, "service-map", environment, windowHours],
    enabled: Boolean(orgId && environment),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/api/v1/service-map", {
          params: { query: { environment: environment!, window_hours: windowHours } },
          signal,
        }),
      ),
    retry: shouldRetry,
  });
}

export function useMembers(orgId: string | undefined) {
  return useQuery({
    queryKey: [orgId, "members"],
    enabled: Boolean(orgId),
    queryFn: ({ signal }) => unwrap(api.GET("/api/v1/orgs/{org_id}/members", { params: { path: { org_id: orgId! } }, signal })),
    retry: shouldRetry,
  });
}

export function useInvites(orgId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: [orgId, "invites"],
    enabled: Boolean(orgId) && enabled,
    queryFn: ({ signal }) => unwrap(api.GET("/api/v1/orgs/{org_id}/invites", { params: { path: { org_id: orgId! } }, signal })),
    retry: shouldRetry,
  });
}

export function useApiKeys(orgId: string | undefined) {
  return useQuery({
    queryKey: [orgId, "api-keys"],
    enabled: Boolean(orgId),
    queryFn: ({ signal }) => unwrap(api.GET("/api/v1/orgs/{org_id}/api-keys", { params: { path: { org_id: orgId! } }, signal })),
    retry: shouldRetry,
  });
}

export function useProjects(orgId: string | undefined) {
  return useQuery({
    queryKey: [orgId, "projects"],
    enabled: Boolean(orgId),
    queryFn: ({ signal }) => unwrap(api.GET("/api/v1/orgs/{org_id}/projects", { params: { path: { org_id: orgId! } }, signal })),
    retry: shouldRetry,
  });
}

export function useAlertRules(orgId: string | undefined, projectId: string | undefined) {
  return useQuery({
    queryKey: [orgId, "alert-rules", projectId],
    enabled: Boolean(orgId && projectId),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/api/v1/orgs/{org_id}/projects/{project_id}/alert-rules", {
          params: { path: { org_id: orgId!, project_id: projectId! } },
          signal,
        }),
      ),
    retry: shouldRetry,
  });
}

export interface AuditFilters {
  action?: string;
  actor_user_id?: string;
  target_type?: string;
  target_id?: string;
  since?: string;
  until?: string;
}

export function useAuditLog(orgId: string | undefined, filters: AuditFilters) {
  const query = clean({ ...filters });
  return useInfiniteQuery({
    queryKey: [orgId, "audit-log", query],
    enabled: Boolean(orgId),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      unwrap(
        api.GET("/api/v1/orgs/{org_id}/audit-log", {
          params: { path: { org_id: orgId! }, query: { ...query, limit: PAGE_SIZE, cursor: pageParam } },
          signal,
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    retry: shouldRetry,
  });
}

export function useBilling(orgId: string | undefined) {
  return useQuery({
    queryKey: [orgId, "billing"],
    enabled: Boolean(orgId),
    queryFn: ({ signal }) => unwrap(api.GET("/api/v1/orgs/{org_id}/billing", { params: { path: { org_id: orgId! } }, signal })),
    retry: shouldRetry,
  });
}

/* ---------------------------------------------------------------- risk (M5) */

/** True when the error means "this endpoint does not exist on this API build yet". */
export function isEndpointMissing(error: unknown): boolean {
  // Starlette's router answers an unknown route with detail "Not Found" (405 for a known path
  // without that method); a handler's own 404 carries its own message.
  return (
    error instanceof ApiError &&
    ((error.status === 404 && error.code === "not_found" && error.message === "Not Found") ||
      (error.status === 405 && error.code === "method_not_allowed"))
  );
}

export interface RiskFilters {
  repository?: string;
  risk_band?: string;
}

export function useRiskAssessments(orgId: string | undefined, filters: RiskFilters = {}) {
  const { repository } = clean({ repository: filters.repository });
  const band = riskBand(filters.risk_band?.trim().toUpperCase());
  const query = { ...(repository ? { repository } : {}), ...(band ? { risk_band: band } : {}) };
  return useInfiniteQuery({
    queryKey: [orgId, "risk-assessments", query],
    enabled: Boolean(orgId),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      unwrap(api.GET("/api/v1/risk/assessments", { params: { query: { ...query, limit: PAGE_SIZE, cursor: pageParam } }, signal })),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    retry: shouldRetry,
  });
}

export function useRiskAssessment(orgId: string | undefined, assessmentId: string | null | undefined) {
  return useQuery({
    queryKey: [orgId, "risk-assessment", assessmentId],
    enabled: Boolean(orgId && assessmentId),
    queryFn: ({ signal }) =>
      unwrap(api.GET("/api/v1/risk/assessments/{assessment_id}", { params: { path: { assessment_id: assessmentId! } }, signal })),
    retry: shouldRetry,
  });
}

export function useRiskModelStatus(orgId: string | undefined) {
  return useQuery({
    queryKey: [orgId, "risk-model"],
    enabled: Boolean(orgId),
    queryFn: ({ signal }) => unwrap(api.GET("/api/v1/risk/model", { signal })),
    retry: shouldRetry,
    staleTime: 5 * 60_000,
  });
}

/** CI runs of one repository (the CI result shown next to, never inside, a risk score). */
export function useCiRuns(orgId: string | undefined, repository: string | null | undefined) {
  return useQuery({
    queryKey: [orgId, "ci-runs", repository],
    enabled: Boolean(orgId && repository && repository.length >= 3),
    queryFn: ({ signal }) => unwrap(api.GET("/api/v1/ci-runs", { params: { query: { repository: repository!, limit: 200 } }, signal })),
    retry: shouldRetry,
  });
}

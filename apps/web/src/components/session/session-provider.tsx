"use client";

import { useQueryClient } from "@tanstack/react-query";
import { usePathname, useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { UpgradeDialog } from "@/components/session/upgrade-dialog";
import { setApiHandlers } from "@/lib/api/client";
import { ApiError, type PlanLimitDetails } from "@/lib/api/errors";
import { useMe } from "@/lib/api/queries";
import { setActiveOrgId } from "@/lib/api/session-store";
import type { MembershipOut, MeOut, Role } from "@/lib/api/types";
import { logout } from "@/lib/auth";
import { safeStorage } from "@/lib/storage";

export const ACTIVE_ORG_KEY = "rl.activeOrg";
export const ONBOARDING_PATH = "/app/onboarding";

const ROLE_RANK: Record<Role, number> = { viewer: 0, member: 1, admin: 2, owner: 3 };

export function roleAtLeast(role: string | null | undefined, minimum: Role): boolean {
  if (!role || !(role in ROLE_RANK)) return false;
  return ROLE_RANK[role as Role] >= ROLE_RANK[minimum];
}

export type SessionStatus = "loading" | "signed-in" | "signed-out" | "error";

export interface SessionContextValue {
  status: SessionStatus;
  error: unknown;
  me: MeOut | undefined;
  memberships: MembershipOut[];
  activeOrg: MembershipOut | null;
  orgId: string | undefined;
  switchOrg: (orgId: string) => void;
  signOut: () => Promise<void>;
  /** Whether the caller's role in the active org is at least `minimum` (UI hint; the API enforces). */
  can: (minimum: Role) => boolean;
  retry: () => void;
  showPlanLimit: (details: PlanLimitDetails) => void;
}

const SessionContext = createContext<SessionContextValue | null>(null);

export function useSession(): SessionContextValue {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSession must be used inside <SessionProvider>");
  return ctx;
}

/** For components that also render outside the dashboard (e.g. tests, marketing). */
export function useOptionalSession(): SessionContextValue | null {
  return useContext(SessionContext);
}

export function loginHref(pathname: string): string {
  return `/login?next=${encodeURIComponent(pathname || "/app")}`;
}

/**
 * Session bootstrap for /app/*: `GET /api/v1/me` (the client refreshes once on 401), the active
 * organization (remembered per browser, validated against the memberships), redirects to /login
 * when there is no session and to onboarding when the user has no organization yet.
 */
export function SessionProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname() ?? "/app";
  const queryClient = useQueryClient();
  const me = useMe();
  const [preferredOrg, setPreferredOrg] = useState<string | null>(() => safeStorage.get(ACTIVE_ORG_KEY));
  const [planLimit, setPlanLimit] = useState<PlanLimitDetails | null>(null);

  const memberships = useMemo(() => me.data?.memberships ?? [], [me.data]);
  const activeOrg = memberships.find((m) => m.org_id === preferredOrg) ?? memberships[0] ?? null;
  // Set synchronously so the first tenant queries already carry X-RL-Org.
  setActiveOrgId(activeOrg?.org_id);

  const unauthorized = me.error instanceof ApiError && me.error.status === 401;
  const status: SessionStatus = me.data ? "signed-in" : unauthorized ? "signed-out" : me.error ? "error" : "loading";

  useEffect(() => {
    return setApiHandlers({
      onSessionExpired: () => {
        queryClient.clear();
        router.replace(loginHref(window.location.pathname));
      },
      onPlanLimit: (details) => setPlanLimit(details),
    });
  }, [queryClient, router]);

  useEffect(() => {
    if (status === "signed-out") router.replace(loginHref(pathname));
  }, [status, pathname, router]);

  useEffect(() => {
    if (status === "signed-in" && memberships.length === 0 && pathname !== ONBOARDING_PATH) router.replace(ONBOARDING_PATH);
  }, [status, memberships.length, pathname, router]);

  const switchOrg = useCallback((orgId: string) => {
    safeStorage.set(ACTIVE_ORG_KEY, orgId);
    setActiveOrgId(orgId);
    setPreferredOrg(orgId);
  }, []);

  const signOut = useCallback(async () => {
    try {
      await logout();
    } finally {
      queryClient.clear();
      router.replace("/login");
    }
  }, [queryClient, router]);

  const value = useMemo<SessionContextValue>(
    () => ({
      status,
      error: me.error,
      me: me.data,
      memberships,
      activeOrg,
      orgId: activeOrg?.org_id,
      switchOrg,
      signOut,
      can: (minimum) => roleAtLeast(activeOrg?.role, minimum),
      retry: () => void me.refetch(),
      showPlanLimit: setPlanLimit,
    }),
    [status, me, memberships, activeOrg, switchOrg, signOut],
  );

  return (
    <SessionContext.Provider value={value}>
      {children}
      <UpgradeDialog details={planLimit} onClose={() => setPlanLimit(null)} canUpgrade={roleAtLeast(activeOrg?.role, "owner")} />
    </SessionContext.Provider>
  );
}

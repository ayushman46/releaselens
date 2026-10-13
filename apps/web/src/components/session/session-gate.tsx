"use client";

import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import { ONBOARDING_PATH, useSession } from "@/components/session/session-provider";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { describeApiError } from "@/lib/api/errors";

export function PageSkeleton({ label = "Loading…" }: { label?: string }) {
  return (
    <div role="status" aria-live="polite">
      <span className="sr-only">{label}</span>
      <Skeleton className="h-7 w-48" />
      <Skeleton className="mt-3 h-4 w-96 max-w-full" />
      <div className="mt-8 grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {Array.from({ length: 4 }, (_, i) => (
          <Skeleton key={i} className="h-24 rounded-xl" />
        ))}
      </div>
      <Skeleton className="mt-6 h-64 rounded-xl" />
    </div>
  );
}

/** Renders the page only once the session is known and an organization is selected. */
export function SessionGate({ children }: { children: ReactNode }) {
  const session = useSession();
  const pathname = usePathname();

  if (session.status === "loading") return <PageSkeleton label="Loading your session…" />;
  if (session.status === "signed-out") return <PageSkeleton label="Redirecting to sign in…" />;
  if (session.status === "error") {
    return (
      <ErrorState
        title="Your session could not be loaded"
        description={describeApiError(session.error)}
        onRetry={session.retry}
      />
    );
  }
  if (!session.activeOrg && pathname !== ONBOARDING_PATH) return <PageSkeleton label="Opening onboarding…" />;
  return <>{children}</>;
}

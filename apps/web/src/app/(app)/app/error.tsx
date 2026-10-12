"use client";

import { ErrorState } from "@/components/ui/error-state";

export default function DashboardError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <ErrorState
      title="This page failed to load"
      description={
        error.digest ? (
          <>
            Try again. If it keeps failing, include reference <span className="font-mono">{error.digest}</span> when
            you report it.
          </>
        ) : (
          "Try again. If it keeps failing, the error below says what went wrong."
        )
      }
      error={error.digest ? undefined : error}
      onRetry={reset}
    />
  );
}

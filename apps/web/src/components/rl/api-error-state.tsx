"use client";

import { ErrorState } from "@/components/ui/error-state";
import { ApiError, describeApiError } from "@/lib/api/errors";

/**
 * An API failure, explained. A 401 here comes after the client already refreshed the session
 * successfully (otherwise the user would have been sent to /login), so it means this route
 * refused the browser session itself rather than "you are signed out".
 */
export function ApiErrorState({
  error,
  onRetry,
  retrying,
  what = "This data",
  className,
}: {
  error: unknown;
  onRetry?: () => void;
  retrying?: boolean;
  what?: string;
  className?: string;
}) {
  let title = `${what}: could not be loaded`;
  let description = describeApiError(error);
  if (error instanceof ApiError) {
    if (error.status === 401) {
      title = `${what}: not available to browser sessions yet`;
      description =
        "You are signed in, but this API endpoint accepts only API keys (read scope) so far, not browser sessions. The data exists; the dashboard can show it once the endpoint accepts sessions with X-RL-Org.";
    } else if (error.status === 403) {
      title = "You don't have access to this";
      description = describeApiError(error);
    } else if (error.status === 404) {
      title = `${what}: not found`;
      description = "It may belong to another organization, or it was deleted by retention. Check the organization in the top bar.";
    }
    if (error.requestId) description = `${description} (request ${error.requestId})`;
  }
  return <ErrorState title={title} description={description} onRetry={onRetry} retrying={retrying} className={className} />;
}

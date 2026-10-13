import { api, apiFetch, unwrap } from "@/lib/api/client";
import { ApiError, parseApiError } from "@/lib/api/errors";
import { setActiveOrgId, setCsrfToken } from "@/lib/api/session-store";
import { API_BASE_URL } from "@/lib/env";

/** Calls behind the auth pages. Each throws {@link ApiError} with the API's code on failure. */

export async function login(email: string, password: string) {
  const session = await unwrap(api.POST("/api/v1/auth/login", { body: { email, password } }));
  setCsrfToken(session.csrf_token);
  return session;
}

export async function signup(email: string, password: string, displayName?: string) {
  return unwrap(
    api.POST("/api/v1/auth/signup", { body: { email, password, display_name: displayName?.trim() || null } }),
  );
}

export async function verifyEmail(token: string) {
  return unwrap(api.POST("/api/v1/auth/verify-email", { body: { token } }));
}

export async function resendVerification(email: string) {
  return unwrap(api.POST("/api/v1/auth/verify-email/resend", { body: { email } }));
}

export async function requestPasswordReset(email: string) {
  return unwrap(api.POST("/api/v1/auth/password-reset/request", { body: { email } }));
}

export async function confirmPasswordReset(token: string, newPassword: string) {
  return unwrap(api.POST("/api/v1/auth/password-reset/confirm", { body: { token, new_password: newPassword } }));
}

export async function acceptInvite(token: string) {
  return unwrap(api.POST("/api/v1/invites/accept", { body: { token } }));
}

/** Revokes the session server-side (works with an expired access token), then forgets local state. */
export async function logout(): Promise<void> {
  try {
    await unwrap(api.POST("/api/v1/auth/logout"));
  } finally {
    setCsrfToken(undefined);
    setActiveOrgId(undefined);
  }
}

export const GITHUB_AUTHORIZE_URL = `${API_BASE_URL}/api/v1/auth/github/authorize`;

/**
 * Checks that GitHub sign-in is configured, then navigates to it. The API answers
 * `503 github_oauth_not_configured` when it is not; a configured server answers a redirect, which
 * `fetch` with `redirect: "manual"` reports as an opaque redirect without following it.
 */
export async function startGithubSignIn(
  navigate: (url: string) => void = (url) => window.location.assign(url),
  fetchImpl: typeof fetch = apiFetch,
): Promise<void> {
  const response = await fetchImpl(GITHUB_AUTHORIZE_URL, { redirect: "manual", credentials: "include" });
  if (response.type === "opaqueredirect" || (response.status >= 300 && response.status < 400) || response.status === 0) {
    navigate(GITHUB_AUTHORIZE_URL);
    return;
  }
  const body: unknown = await response.json().catch(() => undefined);
  throw parseApiError(response.status, body, response.headers);
}

/** Messages for `?error=<code>` from the GitHub callback redirect (rl_api.routers.auth). */
export function githubErrorMessage(code: string | null): string | null {
  if (!code) return null;
  const known: Record<string, string> = {
    github_denied: "GitHub sign-in was cancelled.",
    oauth_state_mismatch: "The GitHub sign-in link was opened in another browser or tab. Try again from here.",
    oauth_state_expired: "The GitHub sign-in link expired. Try again.",
    github_no_verified_email:
      "Your GitHub account has no verified primary e-mail address. Verify one on GitHub, then try again.",
    github_unavailable: "GitHub could not be reached. Try again in a moment.",
    github_bad_response: "GitHub answered unexpectedly. Try again in a moment.",
    github_code_rejected: "GitHub rejected the sign-in. Try again.",
    github_oauth_not_configured: "Sign-in with GitHub is not configured on this server.",
    rate_limited: "Too many sign-in attempts. Wait a minute and try again.",
  };
  return known[code] ?? `GitHub sign-in failed (${code}). Try again, or sign in with e-mail.`;
}

/** E-mailed tokens arrive in the URL fragment (`#token=…`), never in the query string. */
export function readHashToken(hash: string = typeof window === "undefined" ? "" : window.location.hash): string | null {
  const params = new URLSearchParams(hash.replace(/^#/, ""));
  const token = params.get("token");
  return token && token.length <= 128 ? token : null;
}

/** A same-origin path to return to after sign-in; anything else falls back to /app. */
export function safeNext(next: string | null | undefined): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.startsWith("/\\")) return "/app";
  return next;
}

export function isApiErrorCode(error: unknown, code: string): boolean {
  return error instanceof ApiError && error.code === code;
}

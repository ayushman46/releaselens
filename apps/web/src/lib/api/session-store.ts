/**
 * Small in-memory state shared by the API client:
 *
 * - the **CSRF token**. The API sets `rl_csrf` (readable by JS, `Path=/`) and repeats the value
 *   in every login/refresh response (`SessionOut.csrf_token`) for clients that cannot read the
 *   API's cookies. We prefer the cookie (it survives reloads and other tabs' refreshes) and fall
 *   back to the last value the API returned;
 * - the **active organization**, sent as `X-RL-Org` on tenant-implicit routes
 *   (`/api/v1/deployments`, …). Org-scoped routes carry it in the path instead.
 */

export const CSRF_COOKIE = "rl_csrf";
export const CSRF_HEADER = "X-CSRF-Token";
export const ORG_HEADER = "X-RL-Org";

let csrfFromResponse: string | undefined;
let activeOrgId: string | undefined;

export function readCookie(name: string, cookieString?: string): string | undefined {
  const source = cookieString ?? (typeof document === "undefined" ? "" : document.cookie);
  for (const part of source.split(";")) {
    const index = part.indexOf("=");
    if (index === -1) continue;
    if (part.slice(0, index).trim() === name) {
      const raw = part.slice(index + 1).trim();
      try {
        return decodeURIComponent(raw);
      } catch {
        return raw;
      }
    }
  }
  return undefined;
}

export function getCsrfToken(): string | undefined {
  return readCookie(CSRF_COOKIE) || csrfFromResponse;
}

export function setCsrfToken(token: string | undefined): void {
  csrfFromResponse = token || undefined;
}

export function getActiveOrgId(): string | undefined {
  return activeOrgId;
}

export function setActiveOrgId(orgId: string | undefined): void {
  activeOrgId = orgId || undefined;
}

/** Test helper. */
export function resetSessionStore(): void {
  csrfFromResponse = undefined;
  activeOrgId = undefined;
}

export const DEFAULT_API_URL = "http://localhost:28000";

/**
 * Normalises the API base URL: falls back to the local-compose default when unset,
 * blank or not an absolute http(s) URL, and strips trailing slashes.
 */
export function resolveApiBaseUrl(raw: string | undefined): string {
  const value = raw?.trim();
  if (!value) return DEFAULT_API_URL;
  try {
    const url = new URL(value);
    if (url.protocol !== "http:" && url.protocol !== "https:") return DEFAULT_API_URL;
  } catch {
    return DEFAULT_API_URL;
  }
  return value.replace(/\/+$/, "");
}

/**
 * Base URL of the ReleaseLens API as seen from the browser.
 * `process.env.NEXT_PUBLIC_RL_API_URL` must stay a literal member access so Next.js can
 * inline it at build time.
 */
export const API_BASE_URL = resolveApiBaseUrl(process.env.NEXT_PUBLIC_RL_API_URL);

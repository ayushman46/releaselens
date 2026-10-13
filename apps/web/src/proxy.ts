import { NextResponse, type NextRequest } from "next/server";

/**
 * Optimistic check for /app/*: without any ReleaseLens session cookie there is nothing to refresh,
 * so send the visitor to /login straight away instead of rendering the dashboard shell first.
 * The real check is the API's (GET /api/v1/me in the session provider, which also refreshes).
 *
 * `rl_csrf` (Path=/, lives as long as the refresh token) or `rl_access` must be visible to this
 * host: true when the web app and API share a host (any port) or `RL_COOKIE_DOMAIN` names a
 * parent domain. Set RL_WEB_SESSION_COOKIE_CHECK=0 when they don't (e.g. unrelated domains).
 */
export function proxy(request: NextRequest) {
  if (process.env.RL_WEB_SESSION_COOKIE_CHECK === "0") return NextResponse.next();
  const { cookies, nextUrl } = request;
  if (cookies.has("rl_access") || cookies.has("rl_csrf")) return NextResponse.next();
  const login = new URL("/login", nextUrl);
  login.searchParams.set("next", `${nextUrl.pathname}${nextUrl.search}`);
  return NextResponse.redirect(login);
}

export const config = {
  matcher: ["/app", "/app/:path*"],
};

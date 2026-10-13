import { NextRequest } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import { proxy } from "@/proxy";

function request(path: string, cookie?: string) {
  return new NextRequest(new URL(path, "http://localhost:23000"), { headers: cookie ? { cookie } : {} });
}

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("proxy (optimistic /app session check)", () => {
  it("redirects to /login with ?next when no session cookie is present", () => {
    const res = proxy(request("/app/deployments?service=payment"));
    expect(res.status).toBe(307);
    const location = new URL(res.headers.get("location")!);
    expect(location.pathname).toBe("/login");
    expect(location.searchParams.get("next")).toBe("/app/deployments?service=payment");
  });

  it.each(["rl_csrf=abc", "rl_access=rla_x"])("lets the request through with %s", (cookie) => {
    const res = proxy(request("/app", cookie));
    expect(res.headers.get("location")).toBeNull();
    expect(res.headers.get("x-middleware-next")).toBe("1");
  });

  it("can be switched off when the API's cookies are not visible to the web host", () => {
    vi.stubEnv("RL_WEB_SESSION_COOKIE_CHECK", "0");
    expect(proxy(request("/app")).headers.get("location")).toBeNull();
  });
});

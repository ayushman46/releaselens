import { describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api/errors";
import { githubErrorMessage, readHashToken, safeNext, startGithubSignIn } from "@/lib/auth";

describe("auth helpers", () => {
  it("only follows same-origin paths after sign-in", () => {
    expect(safeNext("/app/deployments/1")).toBe("/app/deployments/1");
    expect(safeNext("https://evil.example")).toBe("/app");
    expect(safeNext("//evil.example")).toBe("/app");
    expect(safeNext("/\\evil.example")).toBe("/app");
    expect(safeNext(null)).toBe("/app");
  });

  it("reads e-mailed tokens from the fragment only", () => {
    expect(readHashToken("#token=rlv_abc")).toBe("rlv_abc");
    expect(readHashToken("")).toBeNull();
    expect(readHashToken(`#token=${"x".repeat(200)}`)).toBeNull();
  });

  it("GitHub sign-in: shows 'not configured' on 503 instead of navigating", async () => {
    const navigate = vi.fn();
    const fetchImpl = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ error: { code: "github_oauth_not_configured", message: "sign-in with GitHub is not configured" } }), { status: 503 }),
    );
    const error = await startGithubSignIn(navigate, fetchImpl).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 503, code: "github_oauth_not_configured" });
    expect(navigate).not.toHaveBeenCalled();
    expect(fetchImpl).toHaveBeenCalledWith(expect.stringContaining("/api/v1/auth/github/authorize"), expect.objectContaining({ redirect: "manual" }));
  });

  it("GitHub sign-in: navigates when the API answers with a redirect", async () => {
    const navigate = vi.fn();
    const opaque = { type: "opaqueredirect", status: 0 } as Response;
    await startGithubSignIn(navigate, vi.fn().mockResolvedValue(opaque));
    expect(navigate).toHaveBeenCalledWith(expect.stringContaining("/api/v1/auth/github/authorize"));
  });

  it("explains GitHub callback error codes", () => {
    expect(githubErrorMessage("oauth_state_mismatch")).toMatch(/another browser/);
    expect(githubErrorMessage("something_new")).toMatch(/something_new/);
    expect(githubErrorMessage(null)).toBeNull();
  });
});

import { describe, expect, it } from "vitest";

import { HealthApiError, type ProbeResult, type Readiness } from "@/lib/api/health";
import { livenessState, overallState, readinessState, type QuerySnapshot } from "@/lib/status";

const live: QuerySnapshot<unknown> = { status: "success", data: { data: { status: "ok" } } };

function ready(status: Readiness["status"]): QuerySnapshot<ProbeResult<Readiness>> {
  return {
    status: "success",
    data: { data: { status, checks: {} }, httpStatus: status === "ready" ? 200 : 503, requestId: undefined },
  };
}

const pending = { status: "pending" } as const;
const unreachable = { status: "error", error: new HealthApiError("down", "network") } as const;
const badResponse = { status: "error", error: new HealthApiError("bad", "invalid_response") } as const;

describe("status derivation", () => {
  it("maps individual probes", () => {
    expect(livenessState(live)).toBe("ok");
    expect(livenessState(pending)).toBe("checking");
    expect(livenessState(unreachable)).toBe("unreachable");
    expect(readinessState(ready("not_ready"))).toBe("not_ready");
    expect(readinessState(badResponse)).toBe("error");
  });

  it("treats unknown errors as errors, not as unreachable", () => {
    expect(livenessState({ status: "error", error: new Error("?") })).toBe("error");
  });

  it.each([
    ["ready", live, ready("ready")],
    ["not_ready", live, ready("not_ready")],
    ["checking", pending, ready("ready")],
    ["checking", live, pending],
    ["error", live, badResponse],
    ["unreachable", unreachable, badResponse],
    ["unreachable", live, unreachable],
  ] as const)("is %s", (expected, liveness, readiness) => {
    expect(overallState(liveness, readiness)).toBe(expected);
  });
});

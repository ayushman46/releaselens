import { afterEach, describe, expect, it, vi } from "vitest";

import {
  dependencyLabel,
  fetchLiveness,
  fetchReadiness,
  formatLatency,
  HealthApiError,
  parseLiveness,
  parseReadiness,
  sortedChecks,
} from "@/lib/api/health";

const READY_BODY = {
  status: "ready",
  checks: {
    postgres: { status: "ok", latency_ms: 3.2 },
    kafka: { status: "ok", latency_ms: 41.7 },
  },
};

const NOT_READY_BODY = {
  status: "not_ready",
  checks: {
    postgres: { status: "ok", latency_ms: 2.1 },
    kafka: { status: "error", latency_ms: 2000.4, error: "timed out after 2.0s" },
  },
};

function jsonResponse(body: unknown, status: number, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

function captureError(promise: Promise<unknown>): Promise<unknown> {
  return promise.then(
    () => {
      throw new Error("expected the promise to reject");
    },
    (error: unknown) => error,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("parseLiveness", () => {
  it("accepts the liveness body", () => {
    expect(parseLiveness({ status: "ok" })).toEqual({ status: "ok" });
  });

  it.each([{ status: "down" }, {}, null, "ok"])("rejects %j", (body) => {
    expect(() => parseLiveness(body)).toThrow(HealthApiError);
  });
});

describe("parseReadiness", () => {
  it("accepts a ready body", () => {
    expect(parseReadiness(READY_BODY)).toEqual(READY_BODY);
  });

  it("accepts a not_ready body with per-dependency errors", () => {
    const parsed = parseReadiness(NOT_READY_BODY);
    expect(parsed.status).toBe("not_ready");
    expect(parsed.checks.kafka).toEqual({
      status: "error",
      latency_ms: 2000.4,
      error: "timed out after 2.0s",
    });
  });

  it.each([
    ["an unknown overall status", { status: "degraded", checks: {} }],
    ["an unknown check status", { status: "ready", checks: { postgres: { status: "up", latency_ms: 1 } } }],
    ["a missing latency", { status: "ready", checks: { postgres: { status: "ok" } } }],
    ["a negative latency", { status: "ready", checks: { postgres: { status: "ok", latency_ms: -1 } } }],
    ["a string latency", { status: "ready", checks: { postgres: { status: "ok", latency_ms: "3" } } }],
    ["missing checks", { status: "ready" }],
  ])("rejects %s with an invalid_response error", (_name, body) => {
    try {
      parseReadiness(body);
      expect.unreachable("parseReadiness should have thrown");
    } catch (error) {
      expect(error).toBeInstanceOf(HealthApiError);
      expect((error as HealthApiError).kind).toBe("invalid_response");
      expect((error as HealthApiError).isUnreachable).toBe(false);
      expect((error as HealthApiError).message).toMatch(/^Unexpected response from \/readyz/);
    }
  });
});

describe("helpers", () => {
  it("orders known dependencies first, then the rest alphabetically", () => {
    const order = sortedChecks({
      zookeeper: { status: "ok", latency_ms: 1 },
      kafka: { status: "ok", latency_ms: 1 },
      minio: { status: "ok", latency_ms: 1 },
      postgres: { status: "ok", latency_ms: 1 },
    }).map(([name]) => name);
    expect(order).toEqual(["postgres", "kafka", "minio", "zookeeper"]);
  });

  it("labels known dependencies and passes unknown names through", () => {
    expect(dependencyLabel("postgres")).toBe("PostgreSQL");
    expect(dependencyLabel("kafka")).toBe("Kafka");
    expect(dependencyLabel("redis")).toBe("redis");
  });

  it.each([
    [0, "<1 ms"],
    [0.4, "<1 ms"],
    [3.25, "3.3 ms"],
    [41.7, "42 ms"],
    [2000.4, "2,000 ms"],
  ])("formats %d as %s", (ms, expected) => {
    expect(formatLatency(ms)).toBe(expected);
  });
});

describe("fetchReadiness", () => {
  it("returns a 200 ready body with its request id", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(READY_BODY, 200, { "x-request-id": "req-1" }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await fetchReadiness("http://api.test");

    expect(fetchMock).toHaveBeenCalledWith("http://api.test/readyz", expect.objectContaining({ cache: "no-store" }));
    expect(result).toEqual({ data: READY_BODY, httpStatus: 200, requestId: "req-1" });
  });

  it("treats 503 with a valid body as a not-ready result, not an error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(NOT_READY_BODY, 503)));

    const result = await fetchReadiness("http://api.test");

    expect(result.httpStatus).toBe(503);
    expect(result.data.status).toBe("not_ready");
  });

  it("raises an http error with the API error message for other statuses", async () => {
    const body = { error: { code: "internal_error", message: "boom", request_id: "req-9" } };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(body, 500)));

    const error = await captureError(fetchReadiness("http://api.test"));

    expect(error).toBeInstanceOf(HealthApiError);
    expect(error).toMatchObject({ kind: "http", httpStatus: 500, requestId: "req-9" });
    expect((error as Error).message).toBe("/readyz returned HTTP 500: boom");
  });

  it("raises invalid_response when a 503 body does not match the schema", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("Service Unavailable", { status: 503 })));

    const error = await captureError(fetchReadiness("http://api.test"));

    expect(error).toMatchObject({ kind: "invalid_response" });
  });
});

describe("fetchLiveness", () => {
  it("parses the liveness body", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ status: "ok" }, 200)));

    await expect(fetchLiveness("http://api.test")).resolves.toMatchObject({ data: { status: "ok" } });
  });

  it("maps a failed fetch (connection refused, CORS) to an unreachable network error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    const error = await captureError(fetchLiveness("http://api.test"));

    expect(error).toBeInstanceOf(HealthApiError);
    expect(error).toMatchObject({ kind: "network", isUnreachable: true });
    expect((error as Error).message).toBe("GET /healthz failed: Failed to fetch");
  });

  it("times out when the API does not answer", async () => {
    vi.useFakeTimers();
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_url: string, init: RequestInit) =>
          new Promise((_resolve, reject) => {
            init.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
          }),
      ),
    );

    const pending = captureError(fetchLiveness("http://api.test", { timeoutMs: 1_000 }));
    await vi.advanceTimersByTimeAsync(1_000);
    const error = await pending;

    expect(error).toMatchObject({ kind: "timeout", isUnreachable: true });
  });

  it("rethrows caller cancellation untouched", async () => {
    const controller = new AbortController();
    const abortError = new DOMException("Aborted", "AbortError");
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_url: string, init: RequestInit) =>
          new Promise((_resolve, reject) => {
            init.signal?.addEventListener("abort", () => reject(abortError));
          }),
      ),
    );

    const pending = captureError(fetchLiveness("http://api.test", { signal: controller.signal }));
    controller.abort();

    await expect(pending).resolves.toBe(abortError);
  });
});

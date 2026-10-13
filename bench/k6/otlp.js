// POST /v1/traces (OTLP/HTTP JSON) at a constant rate, SPANS spans per request.
// Spans per second accepted = rate x SPANS when every request succeeds.
import http from "k6/http";
import { check } from "k6";
import { API, constantRate, headers, hex, summarize, summaryTrendStats } from "./common.js";

const RATE = parseInt(__ENV.RATE || "40", 10);
const SPANS = parseInt(__ENV.SPANS || "50", 10);
export const options = {
  discardResponseBodies: true,
  scenarios: { otlp: constantRate(RATE) },
  summaryTrendStats,
  thresholds: { http_req_failed: ["rate<0.01"] },
};

function span(traceId, i, nowNs) {
  const start = nowNs - BigInt(5_000_000 + i * 1000);
  return {
    traceId,
    spanId: hex(16),
    name: "POST /charge",
    kind: 2,
    startTimeUnixNano: start.toString(),
    endTimeUnixNano: (start + BigInt(3_000_000 + (i % 7) * 100_000)).toString(),
    status: { code: i % 50 === 0 ? 2 : 1 },
    attributes: [{ key: "http.route", value: { stringValue: "/charge" } }],
  };
}

export default function () {
  const nowNs = BigInt(Date.now()) * 1_000_000n;
  const spans = [];
  for (let i = 0; i < SPANS; i++) spans.push(span(hex(32), i, nowNs));
  const body = JSON.stringify({
    resourceSpans: [
      {
        resource: {
          attributes: [
            { key: "service.name", value: { stringValue: `bench-svc-${Math.floor(Math.random() * 5)}` } },
            { key: "service.version", value: { stringValue: "v1" } },
            { key: "deployment.environment", value: { stringValue: "bench" } },
          ],
        },
        scopeSpans: [{ scope: { name: "k6" }, spans }],
      },
    ],
  });
  const res = http.post(`${API}/v1/traces`, body, { headers: headers() });
  check(res, { "2xx": (r) => r.status >= 200 && r.status < 300 });
}

export function handleSummary(data) {
  return summarize("otlp", data);
}

// POST /api/v1/deployments at a constant rate: validation, auth, idempotency and the
// transactional outbox write (the Kafka hop happens after the response).
import http from "k6/http";
import { check } from "k6";
import { API, constantRate, headers, hex, summarize, summaryTrendStats } from "./common.js";

const RATE = parseInt(__ENV.RATE || "10", 10);
export const options = {
  discardResponseBodies: true,
  scenarios: { deployments: constantRate(RATE) },
  summaryTrendStats,
  thresholds: { http_req_failed: ["rate<0.01"] },
};

export default function () {
  const body = JSON.stringify({
    service: `bench-${Math.floor(Math.random() * 20)}`,
    environment: "bench",
    version: `v${hex(6)}`,
    commit_sha: hex(40),
    deployed_at: new Date().toISOString(),
    deployer: "k6",
  });
  const res = http.post(`${API}/api/v1/deployments`, body, { headers: headers() });
  check(res, { accepted: (r) => r.status === 202 || r.status === 200 || r.status === 201 });
}

export function handleSummary(data) {
  return summarize("deployments", data);
}

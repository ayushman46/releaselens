// Shared settings for the ReleaseLens API benchmarks (docs/benchmarks/README.md).
export const API = __ENV.API || "http://api:8000";
export const KEY = __ENV.RL_KEY;
export const DURATION = __ENV.DURATION || "60s";

export function headers(contentType = "application/json") {
  if (!KEY) throw new Error("RL_KEY is required");
  return { "x-releaselens-key": KEY, "content-type": contentType };
}

export function hex(n) {
  let s = "";
  for (let i = 0; i < n; i++) s += "0123456789abcdef"[Math.floor(Math.random() * 16)];
  return s;
}

export function constantRate(rate) {
  return {
    executor: "constant-arrival-rate",
    rate,
    timeUnit: "1s",
    duration: DURATION,
    preAllocatedVUs: Math.max(10, rate * 2),
    maxVUs: Math.max(50, rate * 10),
  };
}

export const summaryTrendStats = ["avg", "med", "p(90)", "p(95)", "p(99)", "max"];

// Written by each script's handleSummary: the full k6 summary as JSON plus a short stdout table.
export function summarize(name, data) {
  const out = __ENV.OUT || `/out/${name}.json`;
  const d = data.metrics.http_req_duration?.values || {};
  const reqs = data.metrics.http_reqs?.values || {};
  const failed = data.metrics.http_req_failed?.values?.rate ?? 0;
  const fmt = (v) => (v === undefined ? "-" : v.toFixed(1));
  const line =
    `${name}: ${reqs.count ?? 0} requests, ${fmt(reqs.rate)} req/s, failed ${(failed * 100).toFixed(2)}%\n` +
    `  latency ms  p50 ${fmt(d.med)}  p90 ${fmt(d["p(90)"])}  p95 ${fmt(d["p(95)"])}  p99 ${fmt(d["p(99)"])}  max ${fmt(d.max)}\n`;
  return { [out]: JSON.stringify(data, null, 1), stdout: line };
}

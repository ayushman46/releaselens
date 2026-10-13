// POST /api/v1/risk/assessments at a constant rate. Every request is a new commit, so nothing
// is served from the 10-minute idempotency window: each one loads history, computes features,
// scores with the loaded model and computes SHAP.
import http from "k6/http";
import { check } from "k6";
import { API, constantRate, headers, hex, summarize, summaryTrendStats } from "./common.js";

const RATE = parseInt(__ENV.RATE || "8", 10);
export const options = {
  discardResponseBodies: true,
  scenarios: { risk: constantRate(RATE) },
  summaryTrendStats,
  thresholds: { http_req_failed: ["rate<0.01"] },
};

export default function () {
  const body = JSON.stringify({
    repository: "bench/risk",
    commit_sha: hex(40),
    event: "push",
    triggered_at: new Date().toISOString(),
    change_facts: {
      src_churn: 120,
      test_churn: 30,
      files_added: 2,
      files_deleted: 0,
      files_modified: 5,
      src_files: 6,
      doc_files: 0,
      other_files: 0,
      num_commits_in_push: 1,
      team_size: 4,
      is_pr: false,
    },
  });
  const res = http.post(`${API}/api/v1/risk/assessments`, body, { headers: headers() });
  check(res, { "2xx": (r) => r.status === 200 || r.status === 201 });
}

export function handleSummary(data) {
  return summarize("risk", data);
}

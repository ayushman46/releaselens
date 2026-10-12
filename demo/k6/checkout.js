// Demo traffic for the ReleaseLens demo system (spec §11.1): a constant arrival rate of
// checkout requests against the gateway. Real HTTP requests → real spans; nothing is simulated
// at the telemetry layer.
//
//   docker compose … run --rm k6 run /scripts/checkout.js \
//     -e RATE=20 -e DURATION=10m -e TARGET=http://gateway:8000
import http from "k6/http";
import { check } from "k6";

const RATE = parseInt(__ENV.RATE || "20", 10); // requests per second
const DURATION = __ENV.DURATION || "5m";
const TARGET = __ENV.TARGET || "http://gateway:8000";

export const options = {
  discardResponseBodies: true,
  scenarios: {
    checkout: {
      executor: "constant-arrival-rate",
      rate: RATE,
      timeUnit: "1s",
      duration: DURATION,
      preAllocatedVUs: Math.max(10, RATE * 2),
      maxVUs: Math.max(50, RATE * 10),
    },
  },
  // No pass/fail thresholds: this is a traffic generator. Failures caused by an injected
  // regression are the signal we want ReleaseLens to detect, not a reason to abort the run.
  summaryTrendStats: ["avg", "med", "p(95)", "p(99)", "max"],
};

export default function () {
  const res = http.get(`${TARGET}/checkout`, { tags: { name: "checkout" }, timeout: "15s" });
  check(res, { "status is 2xx": (r) => r.status >= 200 && r.status < 300 });
}

# 3-minute demo script

Everything shown is produced live by `demo/scenarios/run_demo.sh`: real services, real spans, real
Kafka and Postgres, and the real regression engine. The only scripted part is the injected fault,
`payment:v2` adding +250 ms and about 3.5% errors in its own code path.

## Before recording (about 25 minutes)

```bash
make up-core
docker compose -f infra/compose/compose.yaml --env-file infra/compose/.env --profile data up -d minio mlflow  # risk scoring
KEEP_STACK=1 SCENARIO=payment demo/scenarios/run_demo.sh
```

The script prints the deployment URL. To show the deployment in the UI, add your web user to the
demo org, or sign up and use the org the script printed.

## Recording

| Time | Screen | What to say |
|---|---|---|
| 0:00–0:20 | Landing page | "ReleaseLens answers two questions: how risky does this change look before you deploy, and did this deploy introduce a regression after it." |
| 0:20–0:50 | Risk analysis → an assessment | "The score comes from a model trained on 128,617 labelled real Java CI builds. It explains itself with SHAP. It predicts **CI failure risk**, not outages, and most of its signal is that a broken build tends to stay broken. We say so." |
| 0:50–1:20 | Deployments → payment v1 → v2 | "The CD pipeline sent one deployment event. ReleaseLens never deploys anything." |
| 1:20–2:10 | Deployment detail: timeline, before/after table, probable origin | "We compare the 10 minutes before against the post window. A metric only counts if every statistical gate passes. In the measured run, payment's own self-time p95 went from 104 to 352 ms, its own error rate from 0% to 4.5%, and fraud stayed healthy. So payment is the **probable origin**, with HIGH confidence. We never claim a root cause." |
| 2:10–2:40 | Blast radius / service map | "Orders and gateway are affected upstream. Fraud is downstream and healthy. Inventory isn't on the path. Reachable doesn't mean affected." |
| 2:40–3:00 | Model page / README limitations | "Every number here was measured, and the limitations are listed on the same page." |

## Measured reference run (2026-10-13, payment scenario)

| Item | Value |
|---|---|
| Deployment | payment v1 → v2 at 06:17:53 UTC, env `demo` |
| Traffic | k6 at 20 req/s for 21 min (baseline 11 min) |
| Final verdict | REGRESSED, severity 62.0 (HIGH) |
| Probable origin | payment, HIGH confidence (score 12, margin 13) |
| Evidence | self-time p95 104 → 352 ms (+239%); own error rate 0.00% → 4.50% (p = 1.8e-23); fraud healthy |
| Blast radius | orders, gateway AFFECTED_UPSTREAM; fraud HEALTHY_DOWNSTREAM; inventory not in radius |
| Raw result | `demo/scenarios/out/20261013T060603Z-payment.analysis.json` (git-ignored, regenerate with the script) |

## Measured control run (2026-10-13, `SCENARIO=control`)

`payment` is redeployed **v1 → v1**: the same code, so nothing may alarm.

| Item | Value |
|---|---|
| Deployment | payment v1 → v1 at 13:54:27 UTC, env `demo`, commit `5cc6bc2` (RLS on) |
| Traffic | k6 at 20 req/s for 21 min (baseline 11 min); about 90–99k spans per analysis window |
| early_5m / early_10m / final | **HEALTHY / HEALTHY / HEALTHY**, severity 0.0, no probable origin; all five services HEALTHY in every look |
| Jobs | 3 of 3 succeeded on the first attempt |
| Verdict stored after deploy | +309 s, +616 s and +908 s (scheduled +300, +600 and +900 s, so 9, 16 and 8 s to compute) |
| Raw result | `demo/scenarios/out/20261013T134214Z-control.analysis.json` (git-ignored) |

A first control attempt at 08:12 UTC was **discarded**. I ran testcontainers test suites in the
same 2-CPU VM during its post window, which slowed every service at once. The engine reported
REGRESSED, but named no single probable origin (LOW confidence, all five services as
candidates). That is the honest answer for a fleet-wide slowdown, but it is not a valid
control. The rule is now that nothing else runs in the VM during a live run.

**Pre-deploy risk call in the same run:** 88/100 HIGH from `release_risk_ci_cold/1` (no CI
history in the demo org). The demo feeds the extractor this repository's own HEAD, and this is
a Python repository scored by a Java-trained model. SHAP named "codebase size lower than in
any build in the training data" (`sloc = 0`) as the top driver, so the score was an
out-of-domain artefact. The extractor now reports `sloc` as unknown, with a warning, when the
repository has no source in the model's language. Treat the demo's risk number as an
illustration of the API, not as an assessment of this repository.

## Measured fraud run (2026-10-13, `SCENARIO=fraud`)

`fraud` v1 → v2 adds +250 ms in fraud's own code path. Payment's code is unchanged, but it
calls fraud, so its latency rises too. The engine must blame fraud, not payment.

| Item | Value |
|---|---|
| Deployment | fraud v1 → v2 at 14:21:42 UTC, env `demo`, commit `5cc6bc2` |
| early_5m / early_10m / final | REGRESSED HIGH in all three (severity 46.6 / 46.7 / 46.7); probable origin **fraud**, HIGH confidence in all three |
| Triage | fraud score 10, next best payment 2 (margin 8). Reasons: fraud was the deployed service; its own self-time p95 went 62 → 312 ms (+405%); it has no downstream dependencies; payment, orders and gateway degraded upstream, consistent with propagation |
| Blast radius | payment (depth 1), orders (2), gateway (3): AFFECTED_UPSTREAM; inventory not in the radius |
| Jobs | 3 of 3 succeeded on the first attempt; verdicts stored at +377 s, +613 s and +909 s |
| Raw result | `demo/scenarios/out/20261013T141000Z-fraud.analysis.json` (git-ignored) |

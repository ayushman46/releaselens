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

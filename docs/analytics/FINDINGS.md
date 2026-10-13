# Historical CI analytics: findings (M3b, spec §7.5)

> **Generated file, do not edit by hand.** Every number below is in [`findings.json`](findings.json) (same directory; schema in [`SCHEMA.md`](SCHEMA.md)), which the web Analytics page reads. Regenerate with the command below.

- **Generated on:** 2026-10-13 (UTC) by `python -m rl_analysis report --run-id m3-run1 --duckdb-path data/lake/dbt/releaselens.duckdb`
- **Data version:** dataset `travistorrent-5-3-2016`, md5 `4713d8b73b667a2c375b89c20f531fc0` (gh_lang = 'java', one row per tr_build_id (ADR-0017)). Source: TravisTorrent travistorrent_5_3_2016 (Figshare 10.6084/m9.figshare.19314170, CC BY 4.0; Beller, Gousios & Zaidman, MSR 2017).
- **dbt run (marts and features):** `m3-run1` from `s3://releaselens-data`; feature schema `1.0.0`.
- **Builds:** 128,819 Java builds in 402 projects, 2011-06-20T13:04:06Z → 2015-08-19T02:52:38Z. Labelled: 128,617 (label 1 = failed or errored, 0 = passed; canceled/started excluded; 202 excluded). Statuses: canceled 199, errored 15,267, failed 22,068, passed 91,282, started 3.
- **Overall failure rate:** 29.03% (95% CI 28.78–29.28%, 37,335 of 128,617).
- **Environment:** rl_analysis 0.1.0, feature_schema_version 1.0.0, duckdb 1.5.6, numpy 2.5.4, scipy 1.18.1, statsmodels 0.15.0, polars 1.44.2; report schema 1.0.0.

## How to read this report

- Every finding states its **n**, an **effect size** with a **95% confidence interval** where one exists (Wilson for rates, Newcombe for rate differences, Katz for risk ratios, percentile bootstrap for the median difference, Robins-Breslow-Greenland for Mantel-Haenszel odds ratios), the **test** used and its p-value, raw and Holm-adjusted across the family of tests.
- **Large n:** with 10^4 to 10^5 builds per comparison, differences far too small to matter reach tiny p-values. Read the effect size (Cramér's V with Cohen's labels: < 0.1 negligible, 0.1 small, 0.3 medium, 0.5 large for these k x 2 tables), not the p-value.
- **Correlation is not causation.** This is observational data from public projects. Nothing here says that changing a factor would change the failure rate. The within-project (Mantel-Haenszel) lines show how much of an association survives holding the project fixed; some do not.
- p = `< 1e-300` means the tail probability underflows double precision (the test statistic is enormous), not that p is exactly 0.

## Headlines

| # | Finding | n | Effect size | 95% CI | Magnitude | Test | p (raw) | p (Holm) |
|---:|---|---:|---|---|---|---:|---:|---:|
| 1 | [Failure rate by change size](#1-failure-rate-by-change-size) | 128,617 | Cramér's V 0.042 | n/a | negligible | Pearson χ² | 2.29e-47 | 1.84e-46 |
| 2 | [Source churn vs test churn vs failure](#2-source-churn-vs-test-churn-vs-failure) | 128,617 | Cramér's V 0.048 | n/a | negligible | Pearson χ² | 3.42e-60 | 3.08e-59 |
| 3 | [Previous build outcome → next build outcome](#3-previous-build-outcome--next-build-outcome) | 128,169 | risk difference +58.86 pp | +58.35 pp to +59.37 pp | large | Pearson χ² | < 1e-300 | < 1e-300 |
| 4 | [CI duration: passed vs failed builds](#4-ci-duration-passed-vs-failed-builds) | 128,608 | Cliff's δ 0.078 | n/a | negligible | Mann-Whitney U | 1.21e-107 | n/a |
| 5 | [Failure rate by project](#5-failure-rate-by-project) | 105,512 | Cramér's V 0.539 | n/a | large | Pearson χ² | < 1e-300 | < 1e-300 |
| 6 | [Monthly reliability trend](#6-monthly-reliability-trend) | 128,617 | Cramér's V 0.074 | n/a | negligible | Pearson χ² | 6.14e-122 | 7.36e-121 |
| 7 | [Core-team vs non-core contributors](#7-core-team-vs-non-core-contributors) | 128,617 | risk difference -6.81 pp | -7.43 pp to -6.19 pp | negligible | Pearson χ² | 5.79e-109 | 6.37e-108 |
| 8 | [Pull-request vs push builds](#8-pull-request-vs-push-builds) | 128,617 | risk difference -7.04 pp | -7.70 pp to -6.36 pp | negligible | Pearson χ² | 2.10e-83 | 2.10e-82 |

Magnitude: Cohen's label of the test's Cramér's V (for the duration finding, Romano's label of Cliff's δ). Holm: adjusted over all chi-square tests of the report.

## 1. Failure rate by change size

*Spec §7.5.1. Do builds with more changed lines (source + test) fail more often?* Population: labelled builds; total churn = src_churn + test_churn lines (n = 128,617).

Failure rate is 28.09% (95% CI 27.80–28.38%, n=94,546) for 0–100 changed lines and 36.31% (33.45–39.28%, n=1,041) for >5,000 lines: a difference of +8.22 pp (95% CI +5.34 to +11.20 pp), risk ratio 1.29 (95% CI 1.19–1.40). Rates rise monotonically across the 5 buckets. Pearson χ² = 224.2 (dof 4, n=128,617), p = 2.29e-47; effect size Cramér's V = 0.042 (negligible).

| Total churn (lines) | Builds | Failures | Failure rate | 95% CI (Wilson) |
|---|---:|---:|---:|---:|
| 0–100 | 94,546 | 26,557 | 28.09% | 27.80–28.38% |
| 101–500 | 22,760 | 6,910 | 30.36% | 29.77–30.96% |
| 501–1,000 | 5,338 | 1,725 | 32.32% | 31.07–33.58% |
| 1,001–5,000 | 4,932 | 1,765 | 35.79% | 34.46–37.14% |
| >5,000 | 1,041 | 378 | 36.31% | 33.45–39.28% |

- >5,000 vs 0–100 lines: risk difference +8.22 pp (95% CI +5.34 to +11.20 pp, Newcombe), risk ratio 1.29 (95% CI 1.19–1.40) (Katz).
- Monotonic increase across buckets: yes.

**Caveats.**

- Association, not causation: observational data with no controlled assignment; project, period, author and change type are not held fixed. Large changes may concentrate in particular projects, periods or change types (merges, refactors, dependency updates).
- With n in the tens of thousands even negligible differences reach tiny p-values: read the effect size and its interval, not the p-value.
- Churn counts lines in source and test files only (TravisTorrent definitions, docs/data/FEATURE_DEFINITIONS.md); documentation and other files add no lines.

## 2. Source churn vs test churn vs failure

*Spec §7.5.2. How do source-change size and test changes relate to failure, jointly?* Population: labelled builds; quartiles of src_churn; tests changed = test_churn > 0; test-change ratio = test_churn / (src_churn + test_churn) (n = 128,617).

Source-churn quartile cut points (labelled builds, linear interpolation): 0 / 10 / 77 lines. Builds that change tests (test churn > 0) fail at 31.46% vs 28.02% without (+3.43 pp, 95% CI +2.88 to +3.99 pp). Within source-churn quartiles the tests-changed minus no-test-change difference is: Q1 +6.57 pp (+5.37 to +7.79 pp); Q2 +2.01 pp (+0.54 to +3.52 pp); Q3 +0.86 pp (-0.21 to +1.94 pp); Q4 +2.21 pp (+1.19 to +3.22 pp). By test-change ratio the highest rate is (0, 0.1] (32.49%, n=5,949) and the lowest 0 (source only) (28.21%, n=56,497). The 8-cell table: Pearson χ² = 296.5 (dof 7, n=128,617), p = 3.42e-60; effect size Cramér's V = 0.048 (negligible). Tests changed vs not: Within projects (Mantel-Haenszel over 361 projects with both groups, n=124,774): pooled odds ratio 1.10 (95% CI 1.07–1.14), CMH p = 7.59e-09; crude odds ratio on the same builds 1.17 (95% CI 1.14–1.20) (1.18 over all labelled builds, incl. projects with one group only); the association persists within projects but is weaker than the crude one.

Source-churn cut points (linear interpolation over labelled builds): Q1 ≤ 0 < Q2 ≤ 10 < Q3 ≤ 77 < Q4 lines; 32.19% of builds have src_churn = 0. Failure rate (95% Wilson CI, n) by quartile and test change:

| Src-churn quartile | Test churn = 0 | Test churn > 0 | Difference (95% CI) | Quartile overall |
|---|---:|---:|---:|---:|
| Q1 (src churn ≤ 0) | 27.72% (27.25–28.19%, n=34,413) | 34.29% (33.19–35.41%, n=6,987) | +6.57 pp (+5.37 to +7.79 pp) | 28.83% (n=41,400) |
| Q2 (0 < src churn ≤ 10) | 26.84% (26.23–27.47%, n=19,644) | 28.85% (27.52–30.23%, n=4,294) | +2.01 pp (+0.54 to +3.52 pp) | 27.20% (n=23,938) |
| Q3 (10 < src churn ≤ 77) | 27.88% (27.28–28.48%, n=21,241) | 28.74% (27.85–29.63%, n=9,953) | +0.86 pp (-0.21 to +1.94 pp) | 28.15% (n=31,194) |
| Q4 (src churn > 77) | 30.37% (29.65–31.09%, n=15,612) | 32.57% (31.86–33.29%, n=16,473) | +2.21 pp (+1.19 to +3.22 pp) | 31.50% (n=32,085) |
| all | 28.02% (27.73–28.31%, n=90,910) | 31.46% (30.99–31.93%, n=37,707) | +3.43 pp (+2.88 to +3.99 pp) |  |

- Tests changed vs not, within projects (Mantel-Haenszel, 361 projects with both groups, n=124,774; 41 projects without both groups dropped): pooled odds ratio **1.10** (95% CI 1.07–1.14), CMH χ² = 33.4, p = 7.59e-09 (Holm 2.28e-08); crude odds ratio on the same builds 1.17 (95% CI 1.14–1.20); over all labelled builds 1.18 (95% CI 1.15–1.21).

Test-change ratio = test churn / (source + test churn):

| Test-change ratio | Builds | Failures | Failure rate | 95% CI (Wilson) |
|---|---:|---:|---:|---:|
| no line churn (ratio undefined) ¹ | 34,413 | 9,539 | 27.72% | 27.25–28.19% |
| 0 (source only) | 56,497 | 15,935 | 28.21% | 27.84–28.58% |
| (0, 0.1] | 5,949 | 1,933 | 32.49% | 31.31–33.69% |
| (0.1, 0.3] | 7,384 | 2,393 | 32.41% | 31.35–33.48% |
| (0.3, 1] | 24,374 | 7,535 | 30.91% | 30.34–31.50% |

¹ Ratio undefined (no line churn): shown for completeness, not part of the ratio test.

**Caveats.**

- Association, not causation: observational data with no controlled assignment; project, period, author and change type are not held fixed. Changing tests may go together with larger or riskier changes.
- With n in the tens of thousands even negligible differences reach tiny p-values: read the effect size and its interval, not the p-value.
- Ties: 32.19% of these builds have src_churn = 0, so the quartile groups are cut-point groups of unequal size, not equal quarters.

## 3. Previous build outcome → next build outcome

*Spec §7.5.3. Does a failed previous build go with a failing next build?* Population: labelled builds with a previous build that has an outcome (n = 128,169).

Leakage-safe (previous FINISHED build, rl_core feature prev_build_failed): P(fail | previous failed) = 70.94% (70.47–71.40%, n=36,790) vs P(fail | previous passed) = 12.07% (11.86–12.29%, n=91,379), a difference of +58.86 pp (95% CI +58.35 to +59.37 pp). Naive (previous STARTED build): 72.28% (n=37,183) vs 11.25% (n=90,864), +61.03 pp (95% CI +60.53 to +61.52 pp), larger than the leakage-safe difference. For 28,090 of 128,047 naive pairs (21.94%) the naive predecessor is not known to have finished when the build started (its upper-bound finish, start + summed job time, is not before the build's start), so its outcome may not have existed at prediction time; on those overlapping pairs the difference is +67.67 pp vs +58.89 pp on pairs whose predecessor had finished. The two links give the same previous outcome for 122,245 of 128,001 builds (95.50%). Leakage-safe: Within projects (Mantel-Haenszel over 382 projects with both groups, n=123,014): pooled odds ratio 7.38 (95% CI 7.14–7.63), CMH p = < 1e-300; crude odds ratio on the same builds 14.56 (95% CI 14.12–15.01) (17.78 over all labelled builds, incl. projects with one group only); the association persists within projects but is weaker than the crude one.

| Previous-build link | P(fail | prev failed) | P(fail | prev passed) | Difference (95% CI) | Risk ratio (95% CI) |
|---|---:|---:|---:|---:|
| leakage-safe: previous FINISHED build | 70.94% (70.47–71.40%, n=36,790) | 12.07% (11.86–12.29%, n=91,379) | +58.86 pp (+58.35 to +59.37 pp) | 5.88 (95% CI 5.77–5.99) |
| naive: previous STARTED build | 72.28% (71.82–72.73%, n=37,183) | 11.25% (11.05–11.46%, n=90,864) | +61.03 pp (+60.53 to +61.52 pp) | 6.42 (95% CI 6.30–6.55) |

- Leakage-safe: the project's build with an outcome whose finish is the latest one strictly before this build started (finish = started_at + duration, an upper bound): rl_core.features.history, feature prev_build_failed. Excluded: 448 builds with no finished previous build.
- Naive: the project's build that STARTED just before (started_at, build_id), finished or not: int_build_history.naive_prev_label. Excluded: 402 first builds and 168 builds whose predecessor was canceled/started.
- Leakage-safe, within projects (Mantel-Haenszel, 382 projects with both groups, n=123,014; 20 projects without both groups dropped): pooled odds ratio **7.38** (95% CI 7.14–7.63), CMH χ² = 16,891.2, p = < 1e-300 (Holm < 1e-300); crude odds ratio on the same builds 14.56 (95% CI 14.12–15.01); over all labelled builds 17.78 (95% CI 17.25–18.32).
- Naive, within projects (Mantel-Haenszel, 382 projects with both groups, n=122,890; 19 projects without both groups dropped): pooled odds ratio **8.74** (95% CI 8.45–9.03), CMH χ² = 19,901.4, p = < 1e-300 (Holm < 1e-300); crude odds ratio on the same builds 16.95 (95% CI 16.43–17.48); over all labelled builds 20.57 (95% CI 19.95–21.21).

**Why the two differ: overlapping builds.** Naive pairs split by whether the naive predecessor's upper-bound finish (start + summed job time) was before the build's start. The upper bound overstates the finish, so an overlapping predecessor may in fact have finished; one that is not known to have finished is what the leakage-safe rule excludes:

| Naive predecessor | Pairs | P(fail | prev failed) | P(fail | prev passed) | Difference (95% CI) |
|---|---:|---:|---:|---:|
| predecessor had finished (upper-bound finish < start) | 99,948 | 70.29% (n=27,972) | 11.40% (n=71,976) | +58.89 pp (+58.30 to +59.47 pp) |
| predecessor possibly still running (overlap: upper bound ≥ start) | 28,090 | 78.36% (n=9,202) | 10.69% (n=18,888) | +67.67 pp (+66.71 to +68.61 pp) |
| predecessor finish unknown (no duration) | 9 | 44.44% (n=9) | n/a (n=0) | n/a (n/a) |

- 28,090 of 128,047 naive pairs (21.94%) overlap: the predecessor may still have been running, so the naive feature can use an outcome that did not exist yet at prediction time (label leakage). The leakage-safe link skips any predecessor not known to have finished and takes the build whose (upper-bound) finish is the latest one before the start.
- Overlapping pairs agree more strongly (+67.67 pp vs +58.89 pp), and the naive difference exceeds the leakage-safe one (+61.03 pp vs +58.86 pp); on the naive pairs whose predecessor had finished the difference is +58.89 pp. Overlapping pairs are builds that ran at about the same time, plausibly on nearly the same code state (rapid successive pushes, or one commit built as push and PR), whose predecessor's outcome a predictor may not have had.
- The two links give the same previous outcome for 122,245 of 128,001 builds; they differ for 5,756.

**Caveats.**

- Association, not causation: observational data with no controlled assignment; project, period, author and change type are not held fixed. Consecutive builds share the project, branch state, contributors and infrastructure, so a failure followed by a failure can reflect a broken state that persisted; the association does not show that one build's outcome affects the next.
- Only the leakage-safe association is usable at prediction time (ADR-0006); the naive link uses, for overlapping builds, outcomes that may not yet have been known.
- Finish times are upper bounds (started_at + summed job time), so the leakage-safe rule discards some builds that had in fact finished (conservative).

## 4. CI duration: passed vs failed builds

*Spec §7.5.4. Do failed builds run longer or shorter than passed builds?* Population: labelled builds with a duration (tr_duration = summed job time) (n = 128,608).

Median summed job time: failed 570 s (IQR 185 s–1,692 s, n=37,326) vs passed 420 s (IQR 205 s–1,104 s, n=91,282); median difference 150 s (95% bootstrap CI 134 s to 165.5 s). Mann-Whitney U = 1,836,777,278.5, p = 1.21e-107; Cliff's delta = 0.078 (negligible): P(failed longer) - P(passed longer). Failed builds are more spread out: a lower 25th and a higher 75th percentile than passed builds (consistent with some failures stopping early and others running longer).

| Group | Builds | Median | Q25 | Q75 | IQR |
|---|---:|---:|---:|---:|---:|
| passed | 91,282 | 420 s | 205 s | 1,104 s | 899 s |
| failed (failed + errored) | 37,326 | 570 s | 185 s | 1,692 s | 1,507 s |
|   by status: errored | 15,258 | 623 s | 144 s | 2,137.5 s | 1,993.5 s |
|   by status: failed | 22,068 | 549 s | 213 s | 1,523 s | 1,310 s |
|   by status: passed | 91,282 | 420 s | 205 s | 1,104 s | 899 s |

- Mann-Whitney U (two-sided, asymptotic) = 1,836,777,278.5 (x = failed, y = passed), p = 1.21e-107.
- Cliff's δ = **0.078 (negligible)** = P(failed longer) - P(passed longer); thresholds 0.147 / 0.33 / 0.474 (Romano et al. 2006).
- Median difference (failed - passed) 150 s, 95% percentile bootstrap CI 134 s to 165.5 s (1000 resamples, seed 0).
- Excluded: 9 labelled builds without a duration.

**Caveats.**

- Association, not causation: observational data with no controlled assignment; project, period, author and change type are not held fixed.
- Duration is Travis' tr_duration, the SUM of job times, not wall-clock: builds with more jobs (a larger matrix) are longer by construction.
- Failures may stop early (fail-fast, a broken compile) or run until a time-out, and errored builds often break during setup; durations of failed builds mix these, so the difference need not say anything about the change itself.
- Duration is a post-build observation: never a model input for the current build.

## 5. Failure rate by project

*Spec §7.5.5. How much do failure rates differ between projects?* Population: labelled builds of projects with ≥ 200 labelled builds (n = 105,512).

161 of 402 projects have ≥ 200 labelled builds (105,512 of 128,617 builds). Their failure rates range from 1.82% to 100.00% (median 17.08%, IQR 9.96%–34.30%). Highest: apache/drill 100.00%, apache/sling 100.00%, deeplearning4j/deeplearning4j 86.49%. Lowest: tananaev/traccar 1.82%, iluwatar/java-design-patterns 2.40%, rhuss/docker-maven-plugin 2.58%. 2 of these projects never passed a build (apache/drill (n=419), apache/sling (n=3,797)); 0 never failed. Project vs failure: Pearson χ² = 30,623.1 (dof 160, n=105,512), p = < 1e-300; effect size Cramér's V = 0.539 (large).

161 projects with ≥ 200 labelled builds; 241 smaller projects (23,105 builds) are left out. Distribution of project failure rates: min 1.82%, Q25 9.96%, median 17.08%, Q75 34.30%, max 100.00%.

**Top 10 (highest failure rate)**

| Project | Builds | Failures | Failure rate | 95% CI (Wilson) |
|---|---:|---:|---:|---:|
| `apache/drill` | 419 | 419 | 100.00% | 99.09–100.00% |
| `apache/sling` | 3,797 | 3,797 | 100.00% | 99.90–100.00% |
| `deeplearning4j/deeplearning4j` | 1,666 | 1,441 | 86.49% | 84.77–88.05% |
| `twitter/commons` | 521 | 408 | 78.31% | 74.57–81.64% |
| `thinkaurelius/titan` | 270 | 206 | 76.30% | 70.88–80.98% |
| `geoserver/geoserver` | 958 | 653 | 68.16% | 65.15–71.04% |
| `Unidata/thredds` | 1,646 | 1,049 | 63.73% | 61.38–66.02% |
| `stephanenicolas/robospice` | 359 | 225 | 62.67% | 57.56–67.52% |
| `zhangkaitao/es` | 220 | 134 | 60.91% | 54.33–67.12% |
| `simpligility/android-maven-plugin` | 349 | 210 | 60.17% | 54.95–65.17% |

**Bottom 10 (lowest failure rate)**

| Project | Builds | Failures | Failure rate | 95% CI (Wilson) |
|---|---:|---:|---:|---:|
| `tananaev/traccar` | 494 | 9 | 1.82% | 0.96–3.43% |
| `iluwatar/java-design-patterns` | 208 | 5 | 2.40% | 1.03–5.50% |
| `rhuss/docker-maven-plugin` | 310 | 8 | 2.58% | 1.31–5.01% |
| `albertlatacz/java-repl` | 207 | 6 | 2.90% | 1.34–6.18% |
| `square/wire` | 366 | 11 | 3.01% | 1.69–5.30% |
| `recruit-tech/redpen` | 423 | 13 | 3.07% | 1.80–5.19% |
| `exteso/alf.io` | 267 | 9 | 3.37% | 1.78–6.28% |
| `google/closure-compiler` | 537 | 20 | 3.72% | 2.42–5.68% |
| `twilio/twilio-java` | 211 | 8 | 3.79% | 1.93–7.30% |
| `square/javapoet` | 224 | 9 | 4.02% | 2.13–7.46% |

- Never passed: `apache/drill` (n=419), `apache/sling` (n=3,797); never failed: none.

**Caveats.**

- Association, not causation: observational data with no controlled assignment; project, period, author and change type are not held fixed. Projects differ in size, language tooling, test suites, CI configuration and activity period.
- A project that never passes more plausibly has a CI configuration that never worked (or a data artefact) than only risky changes; its builds add only failed → failed pairs, which inflates pooled (crude) associations such as previous-build → next-build.
- A project's rate pools its whole observed history (2011-2015).

## 6. Monthly reliability trend

*Spec §7.5.5. How does the failure rate move over calendar time?* Population: labelled builds by UTC month of started_at (n = 128,617).

50 months (2011-06 to 2015-08); 9 have fewer than 100 labelled builds and are flagged. Among the other 41, the monthly failure rate ranges from 12.37% (2012-11, n=380) to 34.59% (2012-07, n=318). Month vs failure: Pearson χ² = 702.4 (dof 40, n=128,482), p = 6.14e-122; effect size Cramér's V = 0.074 (negligible).

| Month (UTC) | Builds | Failures | Failure rate | 95% CI (Wilson) | Active projects |
|---|---:|---:|---:|---:|---:|
| 2011-06 ² | 1 | 0 | 0.00% | 0.00–79.35% | 1 |
| 2011-08 ² | 4 | 3 | 75.00% | 30.06–95.44% | 1 |
| 2011-09 ² | 3 | 3 | 100.00% | 43.85–100.00% | 1 |
| 2011-10 ² | 3 | 0 | 0.00% | 0.00–56.15% | 1 |
| 2011-11 ² | 5 | 0 | 0.00% | 0.00–43.45% | 1 |
| 2011-12 ² | 20 | 8 | 40.00% | 21.88–61.34% | 3 |
| 2012-01 ² | 12 | 7 | 58.33% | 31.95–80.67% | 4 |
| 2012-02 ² | 16 | 4 | 25.00% | 10.18–49.50% | 4 |
| 2012-03 ² | 71 | 48 | 67.61% | 56.06–77.34% | 8 |
| 2012-04 | 114 | 28 | 24.56% | 17.57–33.21% | 9 |
| 2012-05 | 225 | 44 | 19.56% | 14.90–25.23% | 13 |
| 2012-06 | 310 | 90 | 29.03% | 24.26–34.32% | 22 |
| 2012-07 | 318 | 110 | 34.59% | 29.58–39.98% | 22 |
| 2012-08 | 214 | 57 | 26.64% | 21.16–32.93% | 28 |
| 2012-09 | 241 | 76 | 31.54% | 26.00–37.65% | 29 |
| 2012-10 | 427 | 66 | 15.46% | 12.34–19.19% | 40 |
| 2012-11 | 380 | 47 | 12.37% | 9.43–16.06% | 35 |
| 2012-12 | 710 | 154 | 21.69% | 18.82–24.87% | 57 |
| 2013-01 | 1,018 | 293 | 28.78% | 26.08–31.64% | 58 |
| 2013-02 | 1,195 | 270 | 22.59% | 20.31–25.05% | 77 |
| 2013-03 | 1,380 | 355 | 25.72% | 23.49–28.10% | 78 |
| 2013-04 | 2,101 | 624 | 29.70% | 27.78–31.69% | 90 |
| 2013-05 | 2,159 | 583 | 27.00% | 25.17–28.92% | 99 |
| 2013-06 | 2,307 | 526 | 22.80% | 21.13–24.56% | 116 |
| 2013-07 | 2,232 | 537 | 24.06% | 22.33–25.88% | 128 |
| 2013-08 | 2,492 | 597 | 23.96% | 22.32–25.67% | 127 |
| 2013-09 | 2,390 | 549 | 22.97% | 21.33–24.70% | 132 |
| 2013-10 | 3,027 | 818 | 27.02% | 25.47–28.63% | 155 |
| 2013-11 | 2,803 | 745 | 26.58% | 24.98–28.25% | 154 |
| 2013-12 | 2,515 | 775 | 30.82% | 29.04–32.65% | 149 |
| 2014-01 | 3,435 | 1,081 | 31.47% | 29.94–33.04% | 176 |
| 2014-02 | 3,605 | 1,242 | 34.45% | 32.92–36.02% | 179 |
| 2014-03 | 4,805 | 1,571 | 32.70% | 31.38–34.03% | 194 |
| 2014-04 | 4,484 | 1,484 | 33.10% | 31.73–34.49% | 205 |
| 2014-05 | 4,428 | 1,355 | 30.60% | 29.26–31.97% | 211 |
| 2014-06 | 5,185 | 1,681 | 32.42% | 31.16–33.71% | 226 |
| 2014-07 | 5,346 | 1,841 | 34.44% | 33.17–35.72% | 233 |
| 2014-08 | 5,302 | 1,547 | 29.18% | 27.97–30.42% | 245 |
| 2014-09 | 5,861 | 1,659 | 28.31% | 27.17–29.47% | 246 |
| 2014-10 | 6,551 | 1,948 | 29.74% | 28.64–30.85% | 264 |
| 2014-11 | 5,387 | 1,514 | 28.10% | 26.92–29.32% | 258 |
| 2014-12 | 5,385 | 1,355 | 25.16% | 24.02–26.34% | 224 |
| 2015-01 | 5,882 | 1,842 | 31.32% | 30.14–32.51% | 222 |
| 2015-02 | 5,279 | 1,619 | 30.67% | 29.44–31.93% | 231 |
| 2015-03 | 6,061 | 1,809 | 29.85% | 28.71–31.01% | 237 |
| 2015-04 | 6,110 | 1,711 | 28.00% | 26.89–29.14% | 233 |
| 2015-05 | 5,292 | 1,341 | 25.34% | 24.19–26.53% | 224 |
| 2015-06 | 5,700 | 1,782 | 31.26% | 30.07–32.48% | 224 |
| 2015-07 | 4,336 | 1,143 | 26.36% | 25.07–27.69% | 195 |
| 2015-08 | 1,490 | 393 | 26.38% | 24.20–28.67% | 141 |

² Fewer than 100 labelled builds: wide interval, excluded from the chi-square test.

**Caveats.**

- Association, not causation: observational data with no controlled assignment; project, period, author and change type are not held fixed. The mix of projects changes from month to month (see the projects column), so a monthly rate also reflects which projects were active, not calendar time alone.
- Months with few builds have wide intervals; flagged months are kept in the series but excluded from the chi-square test.

## 7. Core-team vs non-core contributors

*Spec §7.5.6. Do builds of core-team members' commits fail less often?* Population: labelled builds (n = 128,617).

core team: 27.55% (27.27–27.83%, n=100,690); non-core: 34.36% (33.80–34.92%, n=27,927). Difference -6.81 pp (95% CI -7.43 to -6.19 pp), risk ratio 0.80 (95% CI 0.79–0.82). Pearson χ² = 491.8 (dof 1, n=128,617), p = 5.79e-109; effect size Cramér's V = 0.062 (negligible). Within projects (Mantel-Haenszel over 337 projects with both groups, n=121,067): pooled odds ratio 1.01 (95% CI 0.97–1.06), CMH p = 0.5366; crude odds ratio on the same builds 0.74 (95% CI 0.72–0.76) (0.73 over all labelled builds, incl. projects with one group only); the interval includes 1: no association is detectable within projects.

| Group | Builds | Failures | Failure rate | 95% CI (Wilson) |
|---|---:|---:|---:|---:|
| core team | 100,690 | 27,740 | 27.55% | 27.27–27.83% |
| non-core | 27,927 | 9,595 | 34.36% | 33.80–34.92% |

- core team minus non-core: risk difference -6.81 pp (95% CI -7.43 to -6.19 pp), risk ratio 0.80 (95% CI 0.79–0.82).
- Within projects (Mantel-Haenszel, 337 projects with both groups, n=121,067; 65 projects without both groups dropped): pooled odds ratio **1.01** (95% CI 0.97–1.06), CMH χ² = 0.4, p = 0.5366 (Holm 1.0000); crude odds ratio on the same builds 0.74 (95% CI 0.72–0.76); over all labelled builds 0.73 (95% CI 0.71–0.75).

PR x core-team cells (context, no test):

| Cell | Builds | Failures | Failure rate | 95% CI (Wilson) |
|---|---:|---:|---:|---:|
| push · non-core | 25,690 | 9,033 | 35.16% | 34.58–35.75% |
| push · core team | 84,800 | 24,136 | 28.46% | 28.16–28.77% |
| pull request · non-core | 2,237 | 562 | 25.12% | 23.37–26.96% |
| pull request · core team | 15,890 | 3,604 | 22.68% | 22.04–23.34% |

**Caveats.**

- Association, not causation: observational data with no controlled assignment; project, period, author and change type are not held fixed. Core team = TravisTorrent's gh_by_core_team_member: the build's commit authors all belong to the developers who committed or merged pull requests in the 3 months before the build (docs/data/FEATURE_DEFINITIONS.md); experience, the kind of change and review all differ between the groups.
- With n in the tens of thousands even negligible differences reach tiny p-values: read the effect size and its interval, not the p-value.

## 8. Pull-request vs push builds

*Spec §7.5.6. Do pull-request builds fail more or less often than push builds?* Population: labelled builds (n = 128,617).

pull request: 22.98% (22.38–23.60%, n=18,127); push: 30.02% (29.75–30.29%, n=110,490). Difference -7.04 pp (95% CI -7.70 to -6.36 pp), risk ratio 0.77 (95% CI 0.74–0.79). Pearson χ² = 374.4 (dof 1, n=128,617), p = 2.10e-83; effect size Cramér's V = 0.054 (negligible). Within projects (Mantel-Haenszel over 345 projects with both groups, n=107,589): pooled odds ratio 1.01 (95% CI 0.97–1.07), CMH p = 0.5436; crude odds ratio on the same builds 0.87 (95% CI 0.84–0.91) (0.70 over all labelled builds, incl. projects with one group only); the interval includes 1: no association is detectable within projects.

| Group | Builds | Failures | Failure rate | 95% CI (Wilson) |
|---|---:|---:|---:|---:|
| pull request | 18,127 | 4,166 | 22.98% | 22.38–23.60% |
| push | 110,490 | 33,169 | 30.02% | 29.75–30.29% |

- pull request minus push: risk difference -7.04 pp (95% CI -7.70 to -6.36 pp), risk ratio 0.77 (95% CI 0.74–0.79).
- Within projects (Mantel-Haenszel, 345 projects with both groups, n=107,589; 57 projects without both groups dropped): pooled odds ratio **1.01** (95% CI 0.97–1.07), CMH χ² = 0.4, p = 0.5436 (Holm 1.0000); crude odds ratio on the same builds 0.87 (95% CI 0.84–0.91); over all labelled builds 0.70 (95% CI 0.67–0.72).

PR x core-team cells (context, no test):

| Cell | Builds | Failures | Failure rate | 95% CI (Wilson) |
|---|---:|---:|---:|---:|
| push · non-core | 25,690 | 9,033 | 35.16% | 34.58–35.75% |
| push · core team | 84,800 | 24,136 | 28.46% | 28.16–28.77% |
| pull request · non-core | 2,237 | 562 | 25.12% | 23.37–26.96% |
| pull request · core team | 15,890 | 3,604 | 22.68% | 22.04–23.34% |

**Caveats.**

- Association, not causation: observational data with no controlled assignment; project, period, author and change type are not held fixed. gh_is_pr: a PR build tests GitHub's merge of the PR into its base; 'push' is every non-PR build. The contributor mix differs between the two (series by_core_team).
- With n in the tens of thousands even negligible differences reach tiny p-values: read the effect size and its interval, not the p-value.

## 9. All chi-square tests (Holm-adjusted)

Pearson χ² tests of independence (no continuity correction, `rl_core.stats.chi_square`), 15 computed, 0 not computable. Holm adjustment over this family with `statsmodels.stats.multitest.multipletests(method='holm')`.

| Test | Table | n | χ² | dof | Cramér's V | p (raw) | p (Holm) | V magnitude |
|---|---|---:|---:|---:|---:|---:|---:|---|
| `change_size` | failure x total-churn bucket (5 buckets) | 128,617 | 224.2 | 4 | 0.042 | 2.29e-47 | 1.84e-46 | negligible |
| `src_quartile_x_tests_changed` | failure x (src-churn quartile x tests changed), 8 cells | 128,617 | 296.5 | 7 | 0.048 | 3.42e-60 | 3.08e-59 | negligible |
| `src_churn_quartile` | failure x src-churn quartile | 128,617 | 146.4 | 3 | 0.034 | 1.59e-31 | 9.55e-31 | negligible |
| `tests_changed` | failure x tests changed (test churn > 0) | 128,617 | 152.6 | 1 | 0.034 | 4.67e-35 | 3.27e-34 | negligible |
| `tests_changed_within_q1` | failure x tests changed, within src-churn Q1 | 41,400 | 122.3 | 1 | 0.054 | 1.99e-28 | 9.93e-28 | negligible |
| `tests_changed_within_q2` | failure x tests changed, within src-churn Q2 | 23,938 | 7.2 | 1 | 0.017 | 0.0073 | 0.0146 | negligible |
| `tests_changed_within_q3` | failure x tests changed, within src-churn Q3 | 31,194 | 2.5 | 1 | 0.009 | 0.1155 | 0.1155 | negligible |
| `tests_changed_within_q4` | failure x tests changed, within src-churn Q4 | 32,085 | 18.1 | 1 | 0.024 | 2.11e-05 | 6.32e-05 | negligible |
| `test_change_ratio` | failure x test-change-ratio bucket (4 buckets; builds without line churn excluded) | 94,204 | 124.6 | 3 | 0.036 | 7.82e-27 | 3.13e-26 | negligible |
| `prev_build_leakage_safe` | failure x previous FINISHED build failed (rl_core) | 128,169 | 44,166.9 | 1 | 0.587 | < 1e-300 | < 1e-300 | large |
| `prev_build_naive` | failure x previous STARTED build failed (naive link) | 128,047 | 47,753.7 | 1 | 0.611 | < 1e-300 | < 1e-300 | large |
| `projects` | failure x project (projects with >= 200 labelled builds) | 105,512 | 30,623.1 | 160 | 0.539 | < 1e-300 | < 1e-300 | large |
| `months` | failure x calendar month (months with >= 100 labelled builds) | 128,482 | 702.4 | 40 | 0.074 | 6.14e-122 | 7.36e-121 | negligible |
| `core_team` | failure x core team vs non-core | 128,617 | 491.8 | 1 | 0.062 | 5.79e-109 | 6.37e-108 | negligible |
| `pull_requests` | failure x pull request vs push | 128,617 | 374.4 | 1 | 0.054 | 2.10e-83 | 2.10e-82 | negligible |

³ Smallest expected cell count below 5: the χ² approximation is unreliable.

### Within-project tests (Cochran-Mantel-Haenszel, stratified by project)

A second family, Holm-adjusted separately. Projects in which one of the two groups is empty carry no within-project information and are dropped. The crude OR on the same builds differs from the MH OR only by the stratification; the crude OR over all labelled builds also counts the dropped projects.

| Test | Comparison | n | Projects | MH pooled OR (95% CI) | Crude OR, same builds (95% CI) | Crude OR, all labelled builds (95% CI) | p (raw) | p (Holm) |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| `tests_changed_within_projects` | tests changed vs not, stratified by project | 124,774 | 361 | 1.10 (1.07–1.14) | 1.17 (1.14–1.20) | 1.18 (1.15–1.21) | 7.59e-09 | 2.28e-08 |
| `prev_build_leakage_safe_within_projects` | previous FINISHED build failed vs passed, stratified by project | 123,014 | 382 | 7.38 (7.14–7.63) | 14.56 (14.12–15.01) | 17.78 (17.25–18.32) | < 1e-300 | < 1e-300 |
| `prev_build_naive_within_projects` | previous STARTED build failed vs passed, stratified by project | 122,890 | 382 | 8.74 (8.45–9.03) | 16.95 (16.43–17.48) | 20.57 (19.95–21.21) | < 1e-300 | < 1e-300 |
| `core_team_within_projects` | core team vs non-core, stratified by project | 121,067 | 337 | 1.01 (0.97–1.06) | 0.74 (0.72–0.76) | 0.73 (0.71–0.75) | 0.5366 | 1.0000 |
| `pull_requests_within_projects` | pull request vs push, stratified by project | 107,589 | 345 | 1.01 (0.97–1.07) | 0.87 (0.84–0.91) | 0.70 (0.67–0.72) | 0.5436 | 1.0000 |

## Input checks

Cross-checks between the inputs (each value must be 0; the report is not written otherwise):

- `change_size_buckets_disagreeing_with_mart_change_risk`: 0
- `builds_whose_churn_bucket_disagrees_with_fct_builds`: 0
- `builds_whose_total_churn_is_not_src_plus_test`: 0
- `feature_rows_without_build`: 0
- `feature_rows_whose_label_differs`: 0
- `feature_rows_minus_labelled_builds`: 0
- `months_disagreeing_with_mart_build_reliability`: 0
- Naive-link mirror vs dbt `int_build_history` (`releaselens.duckdb`): 128,819 rows compared, 0 missing, 0 mismatched.

Input files:

| File | Rows | Bytes | sha256 |
|---|---:|---:|---|
| `s3://releaselens-data/marts/fct_builds/run_id=m3-run1/part-00000.parquet` | 128,819 | 3,016,347 | `24c1856820051fc4…` |
| `s3://releaselens-data/features/release_features/schema=1.0.0/run_id=m3-run1/part-00000.parquet` | 128,617 | 7,362,393 | `384f66b66bde7416…` |
| `s3://releaselens-data/marts/mart_build_reliability/run_id=m3-run1/part-00000.parquet` | 6,010 | 81,445 | `929d59961d388696…` |
| `s3://releaselens-data/marts/mart_change_risk/run_id=m3-run1/part-00000.parquet` | 12 | 2,361 | `5ce9ee01ddadc9b1…` |

## Caveats (whole report)

- Association, not causation: observational data with no controlled assignment; project, period, author and change type are not held fixed.
- With n in the tens of thousands even negligible differences reach tiny p-values: read the effect size and its interval, not the p-value.
- Label = failed or errored (ADR-0005): errored builds often break in setup or infrastructure rather than in the tests.
- TravisTorrent Java projects 2011-2015 (Travis CI era); not evidence about any particular team's CI today.

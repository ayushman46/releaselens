# `findings.json` schema (report schema 1.0.0)

`docs/analytics/findings.json` (and its copy at
`s3://releaselens-data/reports/analytics/run_id=<run id>/findings.json`) is written by
`python -m rl_analysis report --run-id <dbt run id>` (`data/analysis`). The web Analytics page
reads it, and `rl_analysis.export.rows_for_postgres` turns it into `analytics_runs` +
`analytics_findings` rows. `FINDINGS.md` is rendered from the same dict.

## Stability rules

- Keys documented here are **stable** within a major `schema_version`. New keys may be added in
  a minor version; renaming or removing a key, or changing its meaning, bumps the major version.
- The file is **deterministic**: the same inputs and library versions give the same bytes (no
  wall-clock time; floats rounded to 12 significant digits; seeded bootstrap). The generation date
  is printed in `FINDINGS.md` only.
- Rates and their bounds are proportions in [0, 1] (not percent). Durations are seconds.
  Differences of proportions are in proportion units (0.0822 = 8.22 percentage points).
- `null` means "not defined" (an empty group, a test that cannot be computed), never 0.
- A p-value of `0.0` is a floating-point underflow (the statistic is enormous), not an exact zero.
- `series` arrays are ordered for charting (bucket order, quartile order, month order); every
  element carries a stable `key`.

## Common objects

**Rate** (`rate` everywhere a failure rate appears): `n` (labelled builds), `failures` (label 1),
`rate` (`failures / n`), `ci95_low`, `ci95_high` (95% Wilson interval, `rl_core.stats`). With
`n = 0` the last three are `null`.

**Effect with interval**: `value`, `ci95_low`, `ci95_high`, `method`:
`newcombe_hybrid_score` (risk difference, group 1 minus group 2), `katz_log` (risk ratio),
`woolf_log` (odds ratio), `percentile_bootstrap` (median difference; plus `n_resamples`, `seed`).
`null` when undefined (empty group; a zero count for ratios).

**Chi-square test** (`chi_square_tests.tests[]`): `key` (stable id, referenced by findings),
`finding`, `description`, `groups` (group keys used, empty groups dropped), `n`, `status`
(`ok` | `not_computable`), and when `ok`: `table_shape`, `chi2`, `dof`, `p_value`,
`p_value_holm`, `cramers_v`, `magnitude` (Cohen's label for w = V·sqrt(min(r,c)−1):
negligible < 0.1 ≤ small < 0.3 ≤ medium < 0.5 ≤ large), `min_expected_count`; when not
computable: `reason`.

**Stratified test** (`stratified_tests.tests[]`, also embedded as `within_projects` in findings):
Mantel-Haenszel odds ratio of group 1 vs group 2 **within projects**: `key`, `finding`,
`description`, `strata` (projects with both groups), `strata_dropped`, `n`, `status`, and when `ok`:
`pooled_odds_ratio`, `ci95_low`, `ci95_high` (Robins-Breslow-Greenland), `cmh_statistic`,
`p_value`, `p_value_holm` (Holm over this family), `method`, `crude_odds_ratio` (effect object:
odds ratio of the pooled table of the **same** builds, i.e. the `strata` projects with both groups,
so it differs from `pooled_odds_ratio` only by the stratification), `crude_odds_ratio_all_projects`
(effect object: pooled table of every labelled build of the comparison, including the
`strata_dropped` projects).

**Headline** (`findings[].headline`, one per finding, also the `analytics_findings` row):
`test_key` (→ `chi_square_tests`, `null` for Mann-Whitney), `test` (`pearson_chi_square` |
`mann_whitney_u`), `effect_name` (`cramers_v` | `risk_difference` | `cliffs_delta`),
`effect_value`, `effect_ci95_low`, `effect_ci95_high` (`null` when the effect has no interval),
`effect_magnitude` (Cohen's label of the test's V; Romano's label of Cliff's delta for
duration), `cramers_v`, `p_value`, `p_value_holm`.

## Top level

| Key | Type | Meaning |
|---|---|---|
| `report` | string | `releaselens-historical-ci-analytics` |
| `schema_version` | string | this document's version (`1.0.0`) |
| `run_id` | string | dbt run id of the marts and features analysed |
| `generated_by` | string | the generation command (options that change the content) |
| `dataset` | object | `key`, `version` (md5 of the source file = data version), `subset`, `source`, `first_started_at`, `last_started_at` (ISO 8601 UTC, labelled builds) |
| `inputs` | object | `root`, `run_id`, `feature_schema_version`, `files.<model>` (`location`, `bytes`, `sha256`, `rows`), `feature_manifest` (dataset, version, label rate, split cut points) |
| `environment` | object | versions: `rl_analysis`, `feature_schema_version`, `duckdb`, `numpy`, `scipy`, `statsmodels`, `polars` |
| `config` | object | `min_project_builds`, `min_month_builds`, `top_n`, `bootstrap_resamples`, `bootstrap_seed`, `confidence` |
| `checks` | object | `cross_checks.<name>`: counts of disagreeing rows between inputs (all must be 0, otherwise no report is written); `naive_link_vs_int_build_history`: `status` (`ok` / `not_run`), `rows`, `missing_rows`, `mismatched_rows`, `duckdb_file` |
| `population` | object | `builds`, `status_counts`, `labelled_builds`, `excluded_without_outcome`, `projects`, `first_started_at`, `last_started_at`, `failure` (rate), `label_rule` |
| `findings` | array | the eight findings below, in this order |
| `chi_square_tests` | object | `family`, `adjustment` (Holm), `computed`, `not_computable`, `tests[]` |
| `stratified_tests` | object | same shape, Cochran-Mantel-Haenszel family |
| `caveats` | array of string | report-wide caveats |

## Every finding

`key`, `spec_ref` (spec §7.5 item), `title`, `question`, `population` (`description`, `n`,
optional `excluded.<reason>` counts), `series` (chart data, below), `tests` (keys into
`chi_square_tests`), `headline`, `summary` (one paragraph with the numbers), `caveats` (always
includes the correlation-is-not-causation caveat), plus finding-specific keys.

| `key` | Specific keys | `series` |
|---|---|---|
| `change_size` | `effects` (`comparison`, `risk_difference`, `risk_ratio`: largest vs smallest bucket; `monotonic_increase`) | `buckets[]`: `key` (`0-100`, `101-500`, `501-1000`, `1001-5000`, `5001+`), `label`, `lower`, `upper` (inclusive line bounds of src + test churn; `null` = open), + rate |
| `src_test_churn` | `src_churn_cutpoints` (`q25`, `q50`, `q75`, `method` = linear, `share_zero`); `effects` (`tests_changed_vs_not`, `tests_changed_vs_not_ratio`, `within_quartile[]` {`quartile`, `risk_difference`, `test`}, `tests_changed_within_projects`) | `cells[]` (`key` = `Q<k>|tests_changed` or `Q<k>|no_test_change`, `quartile`, `quartile_label`, `tests_changed`, + rate), `by_quartile[]` (`key` Q1..Q4, `label`, + rate), `by_tests_changed[]`, `test_change_ratio[]` (`key` `no_line_churn`, `0`, `0-0.1`, `0.1-0.3`, `0.3-1`; `in_test` false for `no_line_churn`) |
| `previous_build` | `leakage_safe` and `naive` (`definition`, `p_fail_given_prev_failed`, `p_fail_given_prev_passed` (rates), `risk_difference`, `risk_ratio`, `test`, `within_projects`); `naive` also `pairs`, `overlapping_pairs`, `overlapping_share`, `by_overlap.{finished_before_start, overlapping, unknown_finish}` (`n`, both rates, `risk_difference`); `agreement` (`compared`, `same_previous_outcome`, `different_previous_outcome`) | `bars[]`: `key` `leakage_safe|prev_failed`, `leakage_safe|prev_passed`, `naive|prev_failed`, `naive|prev_passed`, + rate |
| `duration` | `groups.{passed, failed}` (`n`, `median_s`, `q25_s`, `q75_s`, `iqr_s`); `mann_whitney` (`u`, `p_value`, `n_x` = failed, `n_y` = passed, `cliffs_delta` = P(failed longer) − P(passed longer), `magnitude`); `effects.median_difference_s` (bootstrap) | `by_status[]`: `key` (status), `n`, `median_s`, `q25_s`, `q75_s`, `iqr_s` |
| `projects` | `population.projects`, `population.min_labelled_builds`; `rate_distribution` (`min`, `q25`, `median`, `q75`, `max` of project rates); `always_failing[]`, `never_failing[]` (`key`, `n`) | `top[]` (highest rate first), `bottom[]` (lowest first): `key` = project, + rate; ties broken by project name |
| `monthly_trend` | `population.months`, `low_volume_months`, `low_volume_threshold` | `months[]`: `key` (`YYYY-MM`, UTC), `projects` (active projects), `low_volume` (fewer labelled builds than the threshold: plot, but flag), + rate |
| `core_team` | `effects` (`comparison` = core minus non-core, `risk_difference`, `risk_ratio`, `within_projects`) | `groups[]` (`core_team`, `non_core`), `by_pull_request[]` (`key` `push|core_team` …, `pull_request`, `core_team`, + rate) |
| `pull_requests` | `effects` (`comparison` = PR minus push, …) | `groups[]` (`pull_request`, `push`), `by_core_team[]` (same cells) |

## Postgres rows (`rl_analysis.export.rows_for_postgres`)

`{"analytics_runs": [run], "analytics_findings": [...]}`: column names and types are listed in the
module docstring. Ids are UUIDv5 of `run_id` (and `run_id/finding_key`), so a re-export of the
same run produces the same keys and the load can be an idempotent per-run swap.

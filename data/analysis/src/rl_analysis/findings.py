"""The spec §7.5 analyses, computed from the session tables of :mod:`rl_analysis.inputs`.

Population: Java builds of the primary release with an observed outcome ("labelled": ``label``
1 = failed or errored, 0 = passed; canceled and started builds are excluded, ADR-0005/0017).
DuckDB only counts (integers); every rate, interval, statistic and effect size is computed here in
Python from those counts, so the results do not depend on DuckDB's parallel summation order.

Each ``finding_*`` function returns one finding (see ``docs/analytics/SCHEMA.md``) and registers
its chi-square tests in :class:`Analysis` (one family, Holm-adjusted in :mod:`rl_analysis.report`).
Everything here describes associations in observational data, never causes.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import duckdb
import numpy as np

from rl_analysis import fmt
from rl_analysis.effects import (
    chi_square_test,
    mantel_haenszel,
    odds_ratio,
    quantiles,
    rate,
    risk_difference,
    risk_ratio,
)
from rl_core.stats import bootstrap_quantile_diff, mann_whitney

CAUSATION: Final = (
    "Association, not causation: observational data with no controlled assignment; project, "
    "period, author and change type are not held fixed."
)
LARGE_N: Final = (
    "With n in the tens of thousands even negligible differences reach tiny p-values: read the "
    "effect size and its interval, not the p-value."
)

# The churn buckets as labelled by dbt (int_change_profile.churn_bucket, mart_change_risk.bucket).
DBT_CHURN_LABELS: Final = (
    fmt.span("0", "100"),
    fmt.span("101", "500"),
    fmt.span("501", "1,000"),
    fmt.span("1,001", "5,000"),
    ">5,000",
)
# (key, label, lower, upper) on total churn = src_churn + test_churn lines; bounds inclusive.
CHURN_BUCKETS: Final = (
    ("0-100", DBT_CHURN_LABELS[0], 0, 100),
    ("101-500", DBT_CHURN_LABELS[1], 101, 500),
    ("501-1000", DBT_CHURN_LABELS[2], 501, 1000),
    ("1001-5000", DBT_CHURN_LABELS[3], 1001, 5000),
    ("5001+", DBT_CHURN_LABELS[4], 5001, None),
)
# bucket index 1..5 of CHURN_BUCKETS (0 = unknown churn), shared by two queries
_CHURN_BUCKET_COUNTS: Final = """
SELECT CASE WHEN src_churn + test_churn IS NULL THEN 0
            WHEN src_churn + test_churn <= 100 THEN 1
            WHEN src_churn + test_churn <= 500 THEN 2
            WHEN src_churn + test_churn <= 1000 THEN 3
            WHEN src_churn + test_churn <= 5000 THEN 4 ELSE 5 END AS b,
       churn_bucket, label IS NOT NULL AS labelled, count(*) AS n, sum(label) AS failures
FROM builds
GROUP BY ALL
ORDER BY ALL
"""
# test-change ratio = test_churn / (src_churn + test_churn); undefined without line churn.
TEST_RATIO_BUCKETS: Final = (
    ("no_line_churn", "no line churn (ratio undefined)"),
    ("0", "0 (source only)"),
    ("0-0.1", "(0, 0.1]"),
    ("0.1-0.3", "(0.1, 0.3]"),
    ("0.3-1", "(0.3, 1]"),
)


class CheckError(RuntimeError):
    """The marts disagree with each other: the report is not written."""


@dataclass(frozen=True, slots=True)
class AnalysisConfig:
    min_project_builds: int = 200
    min_month_builds: int = 100
    top_n: int = 10
    bootstrap_resamples: int = 1000
    bootstrap_seed: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "min_project_builds": self.min_project_builds,
            "min_month_builds": self.min_month_builds,
            "top_n": self.top_n,
            "bootstrap_resamples": self.bootstrap_resamples,
            "bootstrap_seed": self.bootstrap_seed,
            "confidence": 0.95,
        }


@dataclass(slots=True)
class Analysis:
    con: duckdb.DuckDBPyConnection
    config: AnalysisConfig = field(default_factory=AnalysisConfig)
    tests: list[dict[str, Any]] = field(default_factory=list)
    stratified: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------ plumbing
    def rows(self, sql: str, params: Sequence[Any] | None = None) -> list[tuple[Any, ...]]:
        return self.con.execute(sql, list(params or [])).fetchall()

    def chi2(
        self, key: str, finding: str, description: str, groups: Sequence[tuple[str, int, int]]
    ) -> dict[str, Any]:
        record = chi_square_test(key, finding, description, groups)
        self.tests.append(record)
        return record

    def within_projects(self, key: str, finding: str, description: str, sql: str) -> dict[str, Any]:
        """Mantel-Haenszel odds ratio of group TRUE vs FALSE, stratified by project.

        ``sql`` returns ``(project, group, n, failures)``; rows with a NULL group are ignored.
        The crude (pooled-table) odds ratio is reported next to it, computed on the SAME builds
        (projects with both groups): the two then differ only by the stratification, so a gap
        between them is between-project heterogeneity. The crude odds ratio over every labelled
        build of the comparison (also projects with one group only, which the stratified
        estimate drops) is kept as ``crude_odds_ratio_all_projects`` for reference.
        """
        per: dict[str, dict[bool, tuple[int, int]]] = {}
        for project, group, n, f in self.rows(sql):
            if group is not None:
                per.setdefault(str(project), {})[bool(group)] = (int(n), int(f or 0))
        strata = []
        for project, groups in sorted(per.items()):
            (n1, f1), (n0, f0) = groups.get(True, (0, 0)), groups.get(False, (0, 0))
            strata.append((project, f1, n1, f0, n0))
        record = mantel_haenszel(key, finding, description, strata)
        record["crude_odds_ratio"] = _crude_odds_ratio([s for s in strata if s[2] and s[4]])
        record["crude_odds_ratio_all_projects"] = _crude_odds_ratio(strata)
        self.stratified.append(record)
        return record

    # ------------------------------------------------------------------ population
    def population(self) -> dict[str, Any]:
        status = {
            str(s): int(n)
            for s, n in self.rows("SELECT status, count(*) FROM builds GROUP BY 1 ORDER BY 1")
        }
        row = self.rows(
            "SELECT count(*), sum(label), count(DISTINCT project), "
            "strftime(min(started_at), '%Y-%m-%dT%H:%M:%SZ'), "
            "strftime(max(started_at), '%Y-%m-%dT%H:%M:%SZ') "
            "FROM builds WHERE label IS NOT NULL"
        )[0]
        n, failures = int(row[0]), int(row[1] or 0)
        total = sum(status.values())
        return {
            "builds": total,
            "status_counts": status,
            "labelled_builds": n,
            "excluded_without_outcome": total - n,
            "projects": int(row[2]),
            "first_started_at": row[3],
            "last_started_at": row[4],
            "failure": rate(failures, n),
            "label_rule": "label 1 = failed or errored, 0 = passed; canceled/started excluded",
        }

    # ------------------------------------------------------------------ checks
    def checks(self) -> dict[str, Any]:
        """Cross-checks between the inputs (each a count of disagreeing rows that must be 0).

        Raises :class:`CheckError` if any is non-zero: inconsistent marts are never reported.
        """
        mine = self._churn_counts()
        mart = {
            str(b): (int(n), int(f))
            for b, n, f in self.rows(
                "SELECT bucket, n_labelled_builds, n_failures FROM change_risk "
                "WHERE dimension = 'churn_bucket'"
            )
        }
        expected = {
            DBT_CHURN_LABELS[i]: mine[k]
            for i, (k, *_rest) in enumerate(CHURN_BUCKETS)
            if mine[k][0]
        }
        bucket_mismatch = sum(
            int(n)
            for b, label, _labelled, n, _f in self.rows(_CHURN_BUCKET_COUNTS)
            if int(b) == 0 or label != DBT_CHURN_LABELS[int(b) - 1]
        )
        total_row = self.rows(
            "SELECT count(*) FILTER (WHERE total_churn IS DISTINCT FROM src_churn + test_churn) "
            "FROM builds"
        )[0]
        label_row = self.rows(
            "SELECT count(*) - count(b.build_id), "
            "count(*) FILTER (WHERE b.label IS DISTINCT FROM f.label), "
            "abs(count(*) - (SELECT count(*) FROM builds WHERE label IS NOT NULL)) "
            "FROM features AS f LEFT JOIN builds AS b USING (build_id)"
        )[0]
        month_row = self.rows(
            "WITH a AS (SELECT started_month AS m, count(*) AS n, sum(label) AS f FROM builds "
            "WHERE label IS NOT NULL GROUP BY 1), "
            "b AS (SELECT month_start AS m, sum(n_labelled_builds) AS n, sum(n_failures) AS f "
            "FROM reliability GROUP BY 1) "
            # a month with only unlabelled builds is in the mart (0 labelled) but not in a
            "SELECT count(*) FILTER (WHERE coalesce(a.n, 0) <> coalesce(b.n, 0) "
            "OR coalesce(a.f, 0) <> coalesce(b.f, 0)) FROM a FULL OUTER JOIN b USING (m)"
        )[0]
        out = {
            "change_size_buckets_disagreeing_with_mart_change_risk": sum(
                1 for k in set(expected) | set(mart) if expected.get(k) != mart.get(k)
            ),
            "builds_whose_churn_bucket_disagrees_with_fct_builds": bucket_mismatch,
            "builds_whose_total_churn_is_not_src_plus_test": int(total_row[0]),
            "feature_rows_without_build": int(label_row[0]),
            "feature_rows_whose_label_differs": int(label_row[1]),
            "feature_rows_minus_labelled_builds": int(label_row[2]),
            "months_disagreeing_with_mart_build_reliability": int(month_row[0]),
        }
        failed = sorted(k for k, v in out.items() if v != 0)
        if failed:
            raise CheckError(f"input cross-checks failed: {failed} ({out})")
        return out

    # ------------------------------------------------------------------ 1. change size
    def _churn_counts(self) -> dict[str, tuple[int, int]]:
        counts = {k: (0, 0) for k, *_ in CHURN_BUCKETS}
        for b, _label, labelled, n, f in self.rows(_CHURN_BUCKET_COUNTS):
            if not labelled:
                continue
            if int(b) == 0:
                raise CheckError(f"{int(n)} labelled builds without churn")
            key = CHURN_BUCKETS[int(b) - 1][0]
            counts[key] = (counts[key][0] + int(n), counts[key][1] + int(f or 0))
        return counts

    def finding_change_size(self) -> dict[str, Any]:
        counts = self._churn_counts()
        series = [
            {"key": k, "label": label, "lower": lo, "upper": hi, **rate(counts[k][1], counts[k][0])}
            for k, label, lo, hi in CHURN_BUCKETS
        ]
        test = self.chi2(
            "change_size",
            "change_size",
            "failure x total-churn bucket (5 buckets)",
            [(s["key"], s["n"], s["failures"]) for s in series],
        )
        filled = [s for s in series if s["n"] > 0]
        first, last = filled[0], filled[-1]
        rd = risk_difference(last["failures"], last["n"], first["failures"], first["n"])
        rr = risk_ratio(last["failures"], last["n"], first["failures"], first["n"])
        rates = [s["rate"] for s in filled]
        monotonic = all(a <= b for a, b in itertools.pairwise(rates))
        n = sum(s["n"] for s in series)
        summary = (
            f"Failure rate is {fmt.pct(first['rate'])} (95% CI "
            f"{fmt.ci(first['ci95_low'], first['ci95_high'])}, n={fmt.count(first['n'])}) for "
            f"{first['label']} changed lines and {fmt.pct(last['rate'])} "
            f"({fmt.ci(last['ci95_low'], last['ci95_high'])}, n={fmt.count(last['n'])}) for "
            f"{last['label']} lines: a difference of {fmt.pp(rd['value'] if rd else None)} "
            f"(95% CI {fmt.pp_ci(rd)}), risk ratio {fmt.ratio(rr)}. Rates "
            f"{'rise monotonically' if monotonic else 'do not rise monotonically'} across the "
            f"{len(filled)} buckets. " + _chi2_sentence(test)
        )
        return {
            "key": "change_size",
            "spec_ref": "7.5.1",
            "title": "Failure rate by change size",
            "question": "Do builds with more changed lines (source + test) fail more often?",
            "population": {
                "description": "labelled builds; total churn = src_churn + test_churn lines",
                "n": n,
            },
            "series": {"buckets": series},
            "effects": {
                "comparison": f"{last['label']} vs {first['label']} lines",
                "risk_difference": rd,
                "risk_ratio": rr,
                "monotonic_increase": monotonic,
            },
            "tests": [test["key"]],
            "headline": _headline(test, "cramers_v"),
            "summary": summary,
            "caveats": [
                CAUSATION + " Large changes may concentrate in particular projects, periods or "
                "change types (merges, refactors, dependency updates).",
                LARGE_N,
                "Churn counts lines in source and test files only (TravisTorrent definitions, "
                "docs/data/FEATURE_DEFINITIONS.md); documentation and other files add no lines.",
            ],
        }

    # ------------------------------------------------------------------ 2. src vs test churn
    def finding_src_test_churn(self) -> dict[str, Any]:
        src = np.asarray(
            [
                r[0]
                for r in self.rows(
                    "SELECT src_churn FROM builds WHERE label IS NOT NULL "
                    "AND src_churn IS NOT NULL ORDER BY src_churn"
                )
            ],
            dtype=np.float64,
        )
        c1, c2, c3 = quantiles(src, (0.25, 0.5, 0.75))
        zero_share = float(np.mean(src == 0)) if src.size else None
        quartile_labels = {
            "Q1": f"src churn ≤ {fmt.bound(c1)}",
            "Q2": f"{fmt.bound(c1)} < src churn ≤ {fmt.bound(c2)}",
            "Q3": f"{fmt.bound(c2)} < src churn ≤ {fmt.bound(c3)}",
            "Q4": f"src churn > {fmt.bound(c3)}",
        }
        rows = self.rows(
            "SELECT CASE WHEN src_churn <= ? THEN 1 WHEN src_churn <= ? THEN 2 "
            "WHEN src_churn <= ? THEN 3 ELSE 4 END AS q, test_churn > 0 AS tests_changed, "
            "count(*), sum(label) FROM builds "
            "WHERE label IS NOT NULL AND src_churn IS NOT NULL AND test_churn IS NOT NULL "
            "GROUP BY ALL ORDER BY ALL",
            [c1, c2, c3],
        )
        cell: dict[tuple[str, bool], tuple[int, int]] = {
            (q, t): (0, 0) for q in quartile_labels for t in (False, True)
        }
        for q, t, n, f in rows:
            cell[(f"Q{int(q)}", bool(t))] = (int(n), int(f or 0))

        cells = [
            {
                "key": f"{q}|{'tests_changed' if t else 'no_test_change'}",
                "quartile": q,
                "quartile_label": quartile_labels[q],
                "tests_changed": t,
                **rate(cell[(q, t)][1], cell[(q, t)][0]),
            }
            for q in quartile_labels
            for t in (False, True)
        ]
        by_quartile = []
        for q, label in quartile_labels.items():
            n = cell[(q, False)][0] + cell[(q, True)][0]
            f = cell[(q, False)][1] + cell[(q, True)][1]
            by_quartile.append({"key": q, "label": label, **rate(f, n)})
        by_tests = []
        for t in (False, True):
            n = sum(cell[(q, t)][0] for q in quartile_labels)
            f = sum(cell[(q, t)][1] for q in quartile_labels)
            by_tests.append(
                {
                    "key": "tests_changed" if t else "no_test_change",
                    "label": "test churn > 0" if t else "test churn = 0",
                    "tests_changed": t,
                    **rate(f, n),
                }
            )

        t_cells = self.chi2(
            "src_quartile_x_tests_changed",
            "src_test_churn",
            "failure x (src-churn quartile x tests changed), 8 cells",
            [(c["key"], c["n"], c["failures"]) for c in cells],
        )
        t_quart = self.chi2(
            "src_churn_quartile",
            "src_test_churn",
            "failure x src-churn quartile",
            [(c["key"], c["n"], c["failures"]) for c in by_quartile],
        )
        t_tests = self.chi2(
            "tests_changed",
            "src_test_churn",
            "failure x tests changed (test churn > 0)",
            [(c["key"], c["n"], c["failures"]) for c in by_tests],
        )
        within = []
        for q in quartile_labels:
            (n0, f0), (n1, f1) = cell[(q, False)], cell[(q, True)]
            test = self.chi2(
                f"tests_changed_within_{q.lower()}",
                "src_test_churn",
                f"failure x tests changed, within src-churn {q}",
                [("no_test_change", n0, f0), ("tests_changed", n1, f1)],
            )
            within.append(
                {
                    "quartile": q,
                    "risk_difference": risk_difference(f1, n1, f0, n0),
                    "test": test["key"],
                }
            )

        mh_tests = self.within_projects(
            "tests_changed_within_projects",
            "src_test_churn",
            "tests changed vs not, stratified by project",
            "SELECT project, test_churn > 0, count(*), sum(label) FROM builds "
            "WHERE label IS NOT NULL AND test_churn IS NOT NULL GROUP BY ALL",
        )
        ratio_rows = self.rows(
            "SELECT CASE WHEN src_churn + test_churn = 0 THEN 0 WHEN test_churn = 0 THEN 1 "
            "WHEN 10 * test_churn <= src_churn + test_churn THEN 2 "
            "WHEN 10 * test_churn <= 3 * (src_churn + test_churn) THEN 3 ELSE 4 END AS b, "
            "count(*), sum(label) FROM builds "
            "WHERE label IS NOT NULL AND src_churn IS NOT NULL AND test_churn IS NOT NULL "
            "GROUP BY 1 ORDER BY 1"
        )
        ratio_counts = {int(b): (int(n), int(f or 0)) for b, n, f in ratio_rows}
        ratio_series = [
            {
                "key": key,
                "label": label,
                "in_test": i > 0,
                **rate(ratio_counts.get(i, (0, 0))[1], ratio_counts.get(i, (0, 0))[0]),
            }
            for i, (key, label) in enumerate(TEST_RATIO_BUCKETS)
        ]
        t_ratio = self.chi2(
            "test_change_ratio",
            "src_test_churn",
            "failure x test-change-ratio bucket (4 buckets; builds without line churn excluded)",
            [(s["key"], s["n"], s["failures"]) for s in ratio_series if s["in_test"]],
        )
        n_total = sum(c["n"] for c in cells)
        rd_tests = risk_difference(
            by_tests[1]["failures"], by_tests[1]["n"], by_tests[0]["failures"], by_tests[0]["n"]
        )
        within_txt = "; ".join(
            f"{w['quartile']} {fmt.pp(w['risk_difference']['value'])} "
            f"({fmt.pp_ci(w['risk_difference'])})"
            for w in within
            if w["risk_difference"]
        )
        hi_ratio = max(
            (s for s in ratio_series if s["in_test"] and s["n"]), key=lambda s: s["rate"]
        )
        lo_ratio = min(
            (s for s in ratio_series if s["in_test"] and s["n"]), key=lambda s: s["rate"]
        )
        summary = (
            f"Source-churn quartile cut points (labelled builds, linear interpolation): "
            f"{fmt.bound(c1)} / {fmt.bound(c2)} / {fmt.bound(c3)} lines. Builds that change tests "
            f"(test churn > 0) fail at {fmt.pct(by_tests[1]['rate'])} vs "
            f"{fmt.pct(by_tests[0]['rate'])} without "
            f"({fmt.pp(rd_tests['value'] if rd_tests else None)}, "
            f"95% CI {fmt.pp_ci(rd_tests)}). Within source-churn quartiles the tests-changed minus "
            f"no-test-change difference is: {within_txt}. By test-change ratio the highest rate is "
            f"{hi_ratio['label']} ({fmt.pct(hi_ratio['rate'])}, n={fmt.count(hi_ratio['n'])}) and "
            f"the lowest {lo_ratio['label']} ({fmt.pct(lo_ratio['rate'])}, "
            f"n={fmt.count(lo_ratio['n'])}). "
            + _chi2_sentence(t_cells, "The 8-cell table: ")
            + " Tests changed vs not: "
            + _mh_sentence(mh_tests)
        )
        return {
            "key": "src_test_churn",
            "spec_ref": "7.5.2",
            "title": "Source churn vs test churn vs failure",
            "question": "How do source-change size and test changes relate to failure, jointly?",
            "population": {
                "description": "labelled builds; quartiles of src_churn; tests changed = "
                "test_churn > 0; test-change ratio = test_churn / (src_churn + test_churn)",
                "n": n_total,
            },
            "src_churn_cutpoints": {
                "q25": c1,
                "q50": c2,
                "q75": c3,
                "method": "linear",
                "share_zero": zero_share,
            },
            "series": {
                "cells": cells,
                "by_quartile": by_quartile,
                "by_tests_changed": by_tests,
                "test_change_ratio": ratio_series,
            },
            "effects": {
                "tests_changed_vs_not": rd_tests,
                "tests_changed_vs_not_ratio": risk_ratio(
                    by_tests[1]["failures"],
                    by_tests[1]["n"],
                    by_tests[0]["failures"],
                    by_tests[0]["n"],
                ),
                "within_quartile": within,
                "tests_changed_within_projects": mh_tests,
            },
            "tests": [
                t_cells["key"],
                t_quart["key"],
                t_tests["key"],
                *[w["test"] for w in within],
                t_ratio["key"],
            ],
            "headline": _headline(t_cells, "cramers_v"),
            "summary": summary,
            "caveats": [
                CAUSATION + " Changing tests may go together with larger or riskier changes.",
                LARGE_N,
                f"Ties: {fmt.pct(zero_share)} of these builds have src_churn = 0, so the quartile "
                "groups are cut-point groups of unequal size, not equal quarters.",
            ],
        }

    # ------------------------------------------------------------------ 3. previous build
    def finding_previous_build(self) -> dict[str, Any]:
        safe: dict[int | None, tuple[int, int]] = {
            (None if p is None else int(p)): (int(n), int(f or 0))
            for p, n, f in self.rows(
                "SELECT f.prev_build_failed, count(*), sum(b.label) FROM builds AS b "
                "JOIN features AS f USING (build_id) WHERE b.label IS NOT NULL GROUP BY 1"
            )
        }
        naive_rows = self.rows(
            "SELECT naive_prev_build_id IS NULL AS first_build, naive_prev_label, "
            "naive_prev_finished_before_start, count(*), sum(label) FROM naive "
            "WHERE label IS NOT NULL GROUP BY ALL"
        )
        naive: dict[int | None, tuple[int, int]] = {0: (0, 0), 1: (0, 0)}
        # timing of the naive predecessor: finished before this start / still running
        # (overlapping) / unknown (no duration, hence no upper-bound finish)
        timing_keys = ("finished_before_start", "overlapping", "unknown_finish")
        split: dict[tuple[int, str], tuple[int, int]] = {
            (p, o): (0, 0) for p in (0, 1) for o in timing_keys
        }
        no_pred = no_outcome = 0
        for first, prev, finished, n, f in naive_rows:
            n, f = int(n), int(f or 0)
            if first:
                no_pred += n
                continue
            if prev is None:
                no_outcome += n
                continue
            p = int(prev)
            naive[p] = (naive[p][0] + n, naive[p][1] + f)
            timing = (
                timing_keys[0] if finished is True else timing_keys[2 if finished is None else 1]
            )
            split[(p, timing)] = (split[(p, timing)][0] + n, split[(p, timing)][1] + f)
        agree_rows = self.rows(
            "SELECT f.prev_build_failed = n.naive_prev_label AS same, count(*) FROM naive AS n "
            "JOIN features AS f USING (build_id) WHERE n.label IS NOT NULL "
            "AND f.prev_build_failed IS NOT NULL AND n.naive_prev_label IS NOT NULL GROUP BY 1"
        )
        agree = {bool(s): int(c) for s, c in agree_rows}

        def block(
            counts: Mapping[int | None, tuple[int, int]], key: str, desc: str
        ) -> dict[str, Any]:
            (n1, f1), (n0, f0) = counts.get(1, (0, 0)), counts.get(0, (0, 0))
            test = self.chi2(
                key, "previous_build", desc, [("prev_failed", n1, f1), ("prev_passed", n0, f0)]
            )
            return {
                "p_fail_given_prev_failed": rate(f1, n1),
                "p_fail_given_prev_passed": rate(f0, n0),
                "risk_difference": risk_difference(f1, n1, f0, n0),
                "risk_ratio": risk_ratio(f1, n1, f0, n0),
                "test": test["key"],
                "_test": test,
            }

        s_block = block(
            safe, "prev_build_leakage_safe", "failure x previous FINISHED build failed (rl_core)"
        )
        n_block = block(
            naive, "prev_build_naive", "failure x previous STARTED build failed (naive link)"
        )
        s_test, n_test = s_block.pop("_test"), n_block.pop("_test")
        s_block["within_projects"] = self.within_projects(
            "prev_build_leakage_safe_within_projects",
            "previous_build",
            "previous FINISHED build failed vs passed, stratified by project",
            "SELECT b.project, f.prev_build_failed = 1, count(*), sum(b.label) FROM builds AS b "
            "JOIN features AS f USING (build_id) WHERE b.label IS NOT NULL "
            "AND f.prev_build_failed IS NOT NULL GROUP BY ALL",
        )
        n_block["within_projects"] = self.within_projects(
            "prev_build_naive_within_projects",
            "previous_build",
            "previous STARTED build failed vs passed, stratified by project",
            "SELECT b.project, n.naive_prev_label = 1, count(*), sum(n.label) FROM naive AS n "
            "JOIN builds AS b USING (build_id) WHERE n.label IS NOT NULL "
            "AND n.naive_prev_label IS NOT NULL GROUP BY ALL",
        )
        n_pairs = naive[0][0] + naive[1][0]
        overlapping = split[(0, "overlapping")][0] + split[(1, "overlapping")][0]
        by_overlap: dict[str, dict[str, Any]] = {
            o: {
                "n": split[(0, o)][0] + split[(1, o)][0],
                "p_fail_given_prev_failed": rate(split[(1, o)][1], split[(1, o)][0]),
                "p_fail_given_prev_passed": rate(split[(0, o)][1], split[(0, o)][0]),
                "risk_difference": risk_difference(
                    split[(1, o)][1], split[(1, o)][0], split[(0, o)][1], split[(0, o)][0]
                ),
            }
            for o in timing_keys
        }
        compared = agree.get(True, 0) + agree.get(False, 0)
        s_rd, n_rd = s_block["risk_difference"], n_block["risk_difference"]
        sf, sp = s_block["p_fail_given_prev_failed"], s_block["p_fail_given_prev_passed"]
        nf, np_ = n_block["p_fail_given_prev_failed"], n_block["p_fail_given_prev_passed"]
        ov, fin = by_overlap["overlapping"], by_overlap["finished_before_start"]
        larger = "larger" if n_rd and s_rd and n_rd["value"] > s_rd["value"] else "not larger"
        summary = (
            f"Leakage-safe (previous FINISHED build, rl_core feature prev_build_failed): "
            f"P(fail | previous failed) = {fmt.pct(sf['rate'])} "
            f"({fmt.ci(sf['ci95_low'], sf['ci95_high'])}, n={fmt.count(sf['n'])}) vs "
            f"P(fail | previous passed) = {fmt.pct(sp['rate'])} "
            f"({fmt.ci(sp['ci95_low'], sp['ci95_high'])}, n={fmt.count(sp['n'])}), a difference "
            f"of {fmt.pp(s_rd['value'] if s_rd else None)} (95% CI {fmt.pp_ci(s_rd)}). Naive "
            f"(previous STARTED build): {fmt.pct(nf['rate'])} (n={fmt.count(nf['n'])}) vs "
            f"{fmt.pct(np_['rate'])} (n={fmt.count(np_['n'])}), "
            f"{fmt.pp(n_rd['value'] if n_rd else None)} (95% CI {fmt.pp_ci(n_rd)}), {larger} "
            f"than the leakage-safe difference. For {fmt.count(overlapping)} of "
            f"{fmt.count(n_pairs)} naive pairs "
            f"({fmt.pct(overlapping / n_pairs if n_pairs else None)}) "
            f"the naive predecessor is not known to have finished when the build started (its "
            f"upper-bound finish, start + summed job time, is not before the build's start), so "
            f"its outcome may not have existed at prediction time; on those overlapping pairs the "
            f"difference is {fmt.pp(_value(ov['risk_difference']))} "
            f"vs {fmt.pp(_value(fin['risk_difference']))} "
            f"on pairs whose predecessor had finished. The two links give the same previous "
            f"outcome for {fmt.count(agree.get(True, 0))} of {fmt.count(compared)} builds "
            f"({fmt.pct(agree.get(True, 0) / compared if compared else None)}). Leakage-safe: "
            + _mh_sentence(s_block["within_projects"])
        )
        return {
            "key": "previous_build",
            "spec_ref": "7.5.3",
            "title": "Previous build outcome → next build outcome",
            "question": "Does a failed previous build go with a failing next build?",
            "population": {
                "description": "labelled builds with a previous build that has an outcome",
                "n": s_block["p_fail_given_prev_failed"]["n"]
                + s_block["p_fail_given_prev_passed"]["n"],
                "excluded": {
                    "leakage_safe_no_finished_previous_build": safe.get(None, (0, 0))[0],
                    "naive_first_build_of_project": no_pred,
                    "naive_previous_build_without_outcome": no_outcome,
                },
            },
            "leakage_safe": {
                "definition": "the project's build with an outcome whose finish is the latest one "
                "strictly before this build started (finish = started_at + duration, an upper "
                "bound): rl_core.features.history, feature prev_build_failed",
                **s_block,
            },
            "naive": {
                "definition": "the project's build that STARTED just before (started_at, "
                "build_id), finished or not: int_build_history.naive_prev_label",
                "pairs": n_pairs,
                "overlapping_pairs": overlapping,
                "overlapping_share": overlapping / n_pairs if n_pairs else None,
                "by_overlap": by_overlap,
                **n_block,
            },
            "agreement": {
                "compared": compared,
                "same_previous_outcome": agree.get(True, 0),
                "different_previous_outcome": agree.get(False, 0),
            },
            "series": {
                "bars": [
                    {"key": "leakage_safe|prev_failed", **sf},
                    {"key": "leakage_safe|prev_passed", **sp},
                    {"key": "naive|prev_failed", **nf},
                    {"key": "naive|prev_passed", **np_},
                ]
            },
            "tests": [s_test["key"], n_test["key"]],
            "headline": _headline(s_test, "risk_difference", s_rd),
            "summary": summary,
            "caveats": [
                CAUSATION + " Consecutive builds share the project, branch state, contributors "
                "and infrastructure, so a failure followed by a failure can reflect a broken "
                "state that persisted; the association does not show that one build's outcome "
                "affects the next.",
                "Only the leakage-safe association is usable at prediction time (ADR-0006); the "
                "naive link uses, for overlapping builds, outcomes that may not yet have been "
                "known.",
                "Finish times are upper bounds (started_at + summed job time), so the "
                "leakage-safe rule discards some builds that had in fact finished (conservative).",
            ],
        }

    # ------------------------------------------------------------------ 4. duration
    def finding_duration(self) -> dict[str, Any]:
        rows = self.rows(
            "SELECT label, status, duration_s FROM builds WHERE label IS NOT NULL "
            "AND duration_s IS NOT NULL ORDER BY label, status, duration_s"
        )
        no_duration = self.rows(
            "SELECT count(*) FROM builds WHERE label IS NOT NULL AND duration_s IS NULL"
        )[0][0]
        passed = np.asarray([r[2] for r in rows if r[0] == 0], dtype=np.float64)
        failed = np.asarray([r[2] for r in rows if r[0] == 1], dtype=np.float64)
        statuses = sorted({str(r[1]) for r in rows})

        def describe(values: np.ndarray[Any, Any]) -> dict[str, Any]:
            if values.size == 0:
                return {"n": 0, "median_s": None, "q25_s": None, "q75_s": None, "iqr_s": None}
            q25, q50, q75 = quantiles(values, (0.25, 0.5, 0.75))
            return {
                "n": int(values.size),
                "median_s": q50,
                "q25_s": q25,
                "q75_s": q75,
                "iqr_s": q75 - q25,
            }

        groups = {"passed": describe(passed), "failed": describe(failed)}
        by_status = [
            {
                "key": s,
                **describe(np.asarray([r[2] for r in rows if r[1] == s], dtype=np.float64)),
            }
            for s in statuses
        ]
        mw: dict[str, Any] | None = None
        boot: dict[str, Any] | None = None
        if passed.size and failed.size:
            res = mann_whitney(failed, passed)
            mw = {
                "test": "mann_whitney_u",
                "x": "failed",
                "y": "passed",
                "u": res.u,
                "p_value": res.p_value,
                "n_x": res.n_x,
                "n_y": res.n_y,
                "cliffs_delta": res.cliffs_delta,
                "magnitude": res.magnitude,
            }
            b = bootstrap_quantile_diff(
                passed,
                failed,
                0.5,
                n_resamples=self.config.bootstrap_resamples,
                seed=self.config.bootstrap_seed,
                chunk=50,
            )
            boot = {
                "value": b.diff,
                "ci95_low": b.low,
                "ci95_high": b.high,
                "method": "percentile_bootstrap",
                "n_resamples": b.n_resamples,
                "seed": b.seed,
            }
        p, f = groups["passed"], groups["failed"]
        summary = (
            f"Median summed job time: failed {fmt.seconds(f['median_s'])} (IQR "
            f"{fmt.span(fmt.seconds(f['q25_s']), fmt.seconds(f['q75_s']))}, "
            f"n={fmt.count(f['n'])}) vs passed {fmt.seconds(p['median_s'])} (IQR "
            f"{fmt.span(fmt.seconds(p['q25_s']), fmt.seconds(p['q75_s']))}, "
            f"n={fmt.count(p['n'])}); median difference "
            f"{fmt.seconds(boot['value']) if boot else 'n/a'} (95% bootstrap CI "
            f"{fmt.seconds(boot['ci95_low']) if boot else 'n/a'} to "
            f"{fmt.seconds(boot['ci95_high']) if boot else 'n/a'}). Mann-Whitney U = "
            f"{fmt.big(mw['u'] if mw else None)}, p = {fmt.pval(mw['p_value'] if mw else None)}; "
            f"Cliff's delta = {fmt.num(mw['cliffs_delta'] if mw else None)} "
            f"({mw['magnitude'] if mw else 'n/a'}): P(failed longer) - P(passed longer)."
            + _spread_sentence(p, f)
        )
        return {
            "key": "duration",
            "spec_ref": "7.5.4",
            "title": "CI duration: passed vs failed builds",
            "question": "Do failed builds run longer or shorter than passed builds?",
            "population": {
                "description": "labelled builds with a duration (tr_duration = summed job time)",
                "n": p["n"] + f["n"],
                "excluded": {"no_duration": int(no_duration)},
            },
            "groups": groups,
            "series": {"by_status": by_status},
            "effects": {"median_difference_s": boot},
            "mann_whitney": mw,
            "tests": [],
            "headline": {
                "test_key": None,
                "test": "mann_whitney_u",
                "effect_name": "cliffs_delta",
                "effect_value": mw["cliffs_delta"] if mw else None,
                "effect_ci95_low": None,
                "effect_ci95_high": None,
                "effect_magnitude": mw["magnitude"] if mw else None,
                "p_value": mw["p_value"] if mw else None,
                "p_value_holm": None,
            },
            "summary": summary,
            "caveats": [
                CAUSATION,
                "Duration is Travis' tr_duration, the SUM of job times, not wall-clock: builds "
                "with more jobs (a larger matrix) are longer by construction.",
                "Failures may stop early (fail-fast, a broken compile) or run until a time-out, "
                "and errored builds often break during setup; durations of failed builds mix "
                "these, so the difference need not say anything about the change itself.",
                "Duration is a post-build observation: never a model input for the current build.",
            ],
        }

    # ------------------------------------------------------------------ 5a. projects
    def finding_projects(self) -> dict[str, Any]:
        cfg = self.config
        per_project = [
            {"key": str(p), **rate(int(f or 0), int(n))}
            for p, n, f in self.rows(
                "SELECT project, count(*), sum(label) FROM builds WHERE label IS NOT NULL "
                "GROUP BY 1 ORDER BY 1"
            )
        ]
        eligible = [p for p in per_project if p["n"] >= cfg.min_project_builds]
        top = sorted(eligible, key=lambda p: (-p["rate"], p["key"]))[: cfg.top_n]
        bottom = sorted(eligible, key=lambda p: (p["rate"], p["key"]))[: cfg.top_n]
        test = self.chi2(
            "projects",
            "projects",
            f"failure x project (projects with >= {cfg.min_project_builds} labelled builds)",
            [(p["key"], p["n"], p["failures"]) for p in eligible],
        )
        n_eligible = sum(p["n"] for p in eligible)
        n_all = sum(p["n"] for p in per_project)
        always = [{"key": p["key"], "n": p["n"]} for p in eligible if p["failures"] == p["n"]]
        never = [{"key": p["key"], "n": p["n"]} for p in eligible if p["failures"] == 0]
        rates = np.asarray([p["rate"] for p in eligible], dtype=np.float64)
        spread = None
        if rates.size:
            q = quantiles(rates, (0.0, 0.25, 0.5, 0.75, 1.0))
            spread = {"min": q[0], "q25": q[1], "median": q[2], "q75": q[3], "max": q[4]}
        summary = (
            f"{len(eligible)} of {len(per_project)} projects have ≥ {cfg.min_project_builds} "
            f"labelled builds ({fmt.count(n_eligible)} of {fmt.count(n_all)} builds). Their "
            f"failure rates range from {fmt.pct(spread['min'] if spread else None)} to "
            f"{fmt.pct(spread['max'] if spread else None)} (median "
            f"{fmt.pct(spread['median'] if spread else None)}, IQR "
            + fmt.span(
                fmt.pct(spread["q25"] if spread else None),
                fmt.pct(spread["q75"] if spread else None),
            )
            + "). Highest: "
            + ", ".join(f"{p['key']} {fmt.pct(p['rate'])}" for p in top[:3])
            + ". Lowest: "
            + ", ".join(f"{p['key']} {fmt.pct(p['rate'])}" for p in bottom[:3])
            + f". {len(always)} of these projects never passed a build ("
            + (", ".join(f"{p['key']} (n={fmt.count(p['n'])})" for p in always) or "none")
            + f"); {len(never)} never failed. "
            + _chi2_sentence(test, "Project vs failure: ")
        )
        return {
            "key": "projects",
            "spec_ref": "7.5.5",
            "title": "Failure rate by project",
            "question": "How much do failure rates differ between projects?",
            "population": {
                "description": f"labelled builds of projects with ≥ {cfg.min_project_builds} "
                "labelled builds",
                "n": n_eligible,
                "projects": len(eligible),
                "min_labelled_builds": cfg.min_project_builds,
                "excluded": {
                    "projects_below_threshold": len(per_project) - len(eligible),
                    "builds_of_projects_below_threshold": n_all - n_eligible,
                },
            },
            "rate_distribution": spread,
            "always_failing": always,
            "never_failing": never,
            "series": {"top": top, "bottom": bottom},
            "tests": [test["key"]],
            "headline": _headline(test, "cramers_v"),
            "summary": summary,
            "caveats": [
                CAUSATION + " Projects differ in size, language tooling, test suites, CI "
                "configuration and activity period.",
                "A project that never passes more plausibly has a CI configuration that never "
                "worked (or a data artefact) than only risky changes; its builds add only "
                "failed → failed pairs, which inflates pooled (crude) associations such as "
                "previous-build → next-build.",
                "A project's rate pools its whole observed history (2011-2015).",
            ],
        }

    # ------------------------------------------------------------------ 5b. monthly trend
    def finding_monthly_trend(self) -> dict[str, Any]:
        cfg = self.config
        months = [
            {
                "key": str(m),
                "projects": int(p),
                "low_volume": int(n) < cfg.min_month_builds,
                **rate(int(f or 0), int(n)),
            }
            for m, n, f, p in self.rows(
                "SELECT strftime(started_month, '%Y-%m'), count(*), sum(label), "
                "count(DISTINCT project) FROM builds WHERE label IS NOT NULL GROUP BY 1 ORDER BY 1"
            )
        ]
        kept = [m for m in months if not m["low_volume"]]
        test = self.chi2(
            "months",
            "monthly_trend",
            f"failure x calendar month (months with >= {cfg.min_month_builds} labelled builds)",
            [(m["key"], m["n"], m["failures"]) for m in kept],
        )
        lo = min(kept, key=lambda m: (m["rate"], m["key"])) if kept else None
        hi = max(kept, key=lambda m: (m["rate"], m["key"])) if kept else None
        summary = (
            f"{len(months)} months ({months[0]['key'] if months else 'n/a'} to "
            f"{months[-1]['key'] if months else 'n/a'}); {len(months) - len(kept)} have fewer than "
            f"{cfg.min_month_builds} labelled builds and are flagged. Among the other "
            f"{len(kept)}, the monthly failure rate ranges from "
            f"{fmt.pct(lo['rate'] if lo else None)} ({lo['key'] if lo else 'n/a'}, n="
            f"{fmt.count(lo['n'] if lo else None)}) to {fmt.pct(hi['rate'] if hi else None)} "
            f"({hi['key'] if hi else 'n/a'}, n={fmt.count(hi['n'] if hi else None)}). "
            + _chi2_sentence(test, "Month vs failure: ")
        )
        return {
            "key": "monthly_trend",
            "spec_ref": "7.5.5",
            "title": "Monthly reliability trend",
            "question": "How does the failure rate move over calendar time?",
            "population": {
                "description": "labelled builds by UTC month of started_at",
                "n": sum(m["n"] for m in months),
                "months": len(months),
                "low_volume_months": len(months) - len(kept),
                "low_volume_threshold": cfg.min_month_builds,
            },
            "series": {"months": months},
            "tests": [test["key"]],
            "headline": _headline(test, "cramers_v"),
            "summary": summary,
            "caveats": [
                CAUSATION + " The mix of projects changes from month to month (see the "
                "projects column), so a monthly rate also reflects which projects were active, "
                "not calendar time alone.",
                "Months with few builds have wide intervals; flagged months are kept in the "
                "series but excluded from the chi-square test.",
            ],
        }

    # ------------------------------------------------------------------ 6. groups
    def _two_groups(
        self,
        *,
        key: str,
        spec_ref: str,
        title: str,
        question: str,
        sql: str,
        by_project_sql: str,
        labels: tuple[str, str],
        keys: tuple[str, str],
        caveat: str,
    ) -> dict[str, Any]:
        result = self.rows(sql)
        counts = {bool(g): (int(n), int(f or 0)) for g, n, f in result if g is not None}
        missing = sum(int(n) for g, n, _ in result if g is None)
        (n1, f1), (n0, f0) = counts.get(True, (0, 0)), counts.get(False, (0, 0))
        series = [
            {"key": keys[0], "label": labels[0], **rate(f1, n1)},
            {"key": keys[1], "label": labels[1], **rate(f0, n0)},
        ]
        test = self.chi2(
            key,
            key,
            f"failure x {labels[0]} vs {labels[1]}",
            [(keys[0], n1, f1), (keys[1], n0, f0)],
        )
        rd = risk_difference(f1, n1, f0, n0)
        rr = risk_ratio(f1, n1, f0, n0)
        mh = self.within_projects(
            f"{key}_within_projects",
            key,
            f"{labels[0]} vs {labels[1]}, stratified by project",
            by_project_sql,
        )
        a, b = series
        summary = (
            f"{labels[0]}: {fmt.pct(a['rate'])} ({fmt.ci(a['ci95_low'], a['ci95_high'])}, "
            f"n={fmt.count(a['n'])}); {labels[1]}: {fmt.pct(b['rate'])} "
            f"({fmt.ci(b['ci95_low'], b['ci95_high'])}, n={fmt.count(b['n'])}). Difference "
            f"{fmt.pp(rd['value'] if rd else None)} (95% CI {fmt.pp_ci(rd)}), risk ratio "
            f"{fmt.ratio(rr)}. " + _chi2_sentence(test) + " " + _mh_sentence(mh)
        )
        return {
            "key": key,
            "spec_ref": spec_ref,
            "title": title,
            "question": question,
            "population": {
                "description": "labelled builds",
                "n": n1 + n0,
                "excluded": {"group_unknown": missing},
            },
            "series": {"groups": series},
            "effects": {
                "comparison": f"{labels[0]} minus {labels[1]}",
                "risk_difference": rd,
                "risk_ratio": rr,
                "within_projects": mh,
            },
            "tests": [test["key"]],
            "headline": _headline(test, "risk_difference", rd),
            "summary": summary,
            "caveats": [CAUSATION + " " + caveat, LARGE_N],
        }

    def finding_core_team(self) -> dict[str, Any]:
        out = self._two_groups(
            key="core_team",
            spec_ref="7.5.6",
            title="Core-team vs non-core contributors",
            question="Do builds of core-team members' commits fail less often?",
            sql="SELECT by_core_team_member, count(*), sum(label) FROM builds "
            "WHERE label IS NOT NULL GROUP BY 1 ORDER BY 1",
            by_project_sql="SELECT project, by_core_team_member, count(*), sum(label) FROM builds "
            "WHERE label IS NOT NULL GROUP BY ALL",
            labels=("core team", "non-core"),
            keys=("core_team", "non_core"),
            caveat="Core team = TravisTorrent's gh_by_core_team_member: the build's commit "
            "authors all belong to the developers who committed or merged pull requests in the "
            "3 months before the build (docs/data/FEATURE_DEFINITIONS.md); experience, the kind "
            "of change and review all differ between the groups.",
        )
        out["series"]["by_pull_request"] = self._cross()
        return out

    def finding_pull_requests(self) -> dict[str, Any]:
        out = self._two_groups(
            key="pull_requests",
            spec_ref="7.5.6",
            title="Pull-request vs push builds",
            question="Do pull-request builds fail more or less often than push builds?",
            sql="SELECT is_pr, count(*), sum(label) FROM builds "
            "WHERE label IS NOT NULL GROUP BY 1 ORDER BY 1",
            by_project_sql="SELECT project, is_pr, count(*), sum(label) FROM builds "
            "WHERE label IS NOT NULL GROUP BY ALL",
            labels=("pull request", "push"),
            keys=("pull_request", "push"),
            caveat="gh_is_pr: a PR build tests GitHub's merge of the PR into its base; 'push' "
            "is every non-PR build. The contributor mix differs between the two (series "
            "by_core_team).",
        )
        out["series"]["by_core_team"] = self._cross()
        return out

    def _cross(self) -> list[dict[str, Any]]:
        """Rates per (PR, core team) cell: context for the two 2x2 comparisons (no test)."""
        return [
            {
                "key": f"{'pull_request' if pr else 'push'}|{'core_team' if core else 'non_core'}",
                "pull_request": bool(pr),
                "core_team": bool(core),
                **rate(int(f or 0), int(n)),
            }
            for pr, core, n, f in self.rows(
                "SELECT is_pr, by_core_team_member, count(*), sum(label) FROM builds "
                "WHERE label IS NOT NULL AND is_pr IS NOT NULL "
                "AND by_core_team_member IS NOT NULL GROUP BY ALL ORDER BY ALL"
            )
        ]


def _value(effect: dict[str, Any] | None) -> float | None:
    return None if not effect else effect.get("value")


def _crude_odds_ratio(strata: Sequence[tuple[str, int, int, int, int]]) -> dict[str, Any] | None:
    """Odds ratio of the pooled table of ``(stratum, failures_1, n_1, failures_2, n_2)``."""
    return odds_ratio(
        sum(s[1] for s in strata),
        sum(s[2] for s in strata),
        sum(s[3] for s in strata),
        sum(s[4] for s in strata),
    )


def _spread_sentence(passed: dict[str, Any], failed: dict[str, Any]) -> str:
    if passed["q25_s"] is None or failed["q25_s"] is None:
        return ""
    lower = failed["q25_s"] < passed["q25_s"]
    higher = failed["q75_s"] > passed["q75_s"]
    if lower and higher:
        return (
            " Failed builds are more spread out: a lower 25th and a higher 75th percentile than "
            "passed builds (consistent with some failures stopping early and others running "
            "longer)."
        )
    return (
        f" Failed builds' 25th percentile is {'lower' if lower else 'not lower'} and their 75th "
        f"percentile {'higher' if higher else 'not higher'} than passed builds'."
    )


def _mh_sentence(rec: dict[str, Any]) -> str:
    if rec.get("status") != "ok":
        return f"Within projects: not computable ({rec.get('reason')})."
    crude = rec.get("crude_odds_ratio")
    if rec["ci95_low"] <= 1.0 <= rec["ci95_high"]:
        reading = "the interval includes 1: no association is detectable within projects"
    elif crude and abs(math.log(rec["pooled_odds_ratio"])) < abs(math.log(crude["value"])):
        reading = "the association persists within projects but is weaker than the crude one"
    else:
        reading = "the association persists within projects"
    return (
        f"Within projects (Mantel-Haenszel over {rec['strata']} projects with both groups, "
        f"n={fmt.count(rec['n'])}): pooled odds ratio {rec['pooled_odds_ratio']:.2f} (95% CI "
        f"{fmt.span(f'{rec["ci95_low"]:.2f}', f'{rec["ci95_high"]:.2f}')}), CMH p = "
        f"{fmt.pval(rec['p_value'])}; crude odds ratio on the same builds {fmt.ratio(crude)}"
        f"{_all_projects_note(rec)}; {reading}."
    )


def _all_projects_note(rec: dict[str, Any]) -> str:
    """The crude odds ratio over all labelled builds, when projects were dropped (else empty)."""
    every = rec.get("crude_odds_ratio_all_projects")
    if not rec.get("strata_dropped") or not every:
        return ""
    return f" ({every['value']:.2f} over all labelled builds, incl. projects with one group only)"


def _chi2_sentence(test: dict[str, Any], prefix: str = "") -> str:
    if test.get("status") != "ok":
        return f"{prefix}chi-square not computable ({test.get('reason')})."
    return (
        f"{prefix}Pearson χ² = {test['chi2']:,.1f} (dof {test['dof']}, n={fmt.count(test['n'])}), "
        f"p = {fmt.pval(test['p_value'])}; effect size Cramér's V = {test['cramers_v']:.3f} "
        f"({test['magnitude']})."
    )


def _headline(
    test: dict[str, Any], effect_name: str, effect: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The finding's headline: its primary test and effect (Holm p filled in by the report)."""
    ok = test.get("status") == "ok"
    if effect_name == "cramers_v":
        value = test.get("cramers_v") if ok else None
        low = high = None
        magnitude = test.get("magnitude") if ok else None
    else:
        value = effect["value"] if effect else None
        low = effect["ci95_low"] if effect else None
        high = effect["ci95_high"] if effect else None
        magnitude = test.get("magnitude") if ok else None
    return {
        "test_key": test["key"],
        "test": "pearson_chi_square",
        "effect_name": effect_name,
        "effect_value": value,
        "effect_ci95_low": low,
        "effect_ci95_high": high,
        "effect_magnitude": magnitude,
        "cramers_v": test.get("cramers_v") if ok else None,
        "p_value": test.get("p_value") if ok else None,
        "p_value_holm": None,
    }

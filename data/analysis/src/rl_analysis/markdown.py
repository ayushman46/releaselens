"""Render ``FINDINGS.md`` from a report dict (every number comes from ``findings.json``)."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from rl_analysis import fmt


def _table(header: Sequence[str], rows: Iterable[Sequence[Any]], align: str = "") -> list[str]:
    align = align or "l" + "r" * (len(header) - 1)
    marks = {"l": "---", "r": "---:", "c": ":---:"}
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(marks[a] for a in align) + "|"]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return [*out, ""]


def _value(effect: Mapping[str, Any] | None) -> float | None:
    return None if not effect else effect.get("value")


def _rate_cells(r: Mapping[str, Any]) -> list[str]:
    return [
        fmt.count(r["n"]),
        fmt.count(r["failures"]),
        fmt.pct(r["rate"]),
        fmt.ci(r["ci95_low"], r["ci95_high"]),
    ]


_RATE_HEAD = ("Builds", "Failures", "Failure rate", "95% CI (Wilson)")


def _effect_cell(h: Mapping[str, Any]) -> tuple[str, str]:
    name, value = h["effect_name"], h["effect_value"]
    if value is None:
        return "n/a", "n/a"
    if name == "risk_difference":
        return f"risk difference {fmt.pp(value)}", (
            f"{fmt.pp(h['effect_ci95_low'])} to {fmt.pp(h['effect_ci95_high'])}"
        )
    if name == "cliffs_delta":
        return f"Cliff's δ {value:.3f}", "n/a"
    return f"Cramér's V {value:.3f}", "n/a"


def _caveats(f: Mapping[str, Any]) -> list[str]:
    return ["**Caveats.**", ""] + [f"- {c}" for c in f["caveats"]] + [""]


def _mh_line(rec: Mapping[str, Any] | None) -> str:
    if not rec or rec.get("status") != "ok":
        return "- Within projects: not computable."
    crude = rec.get("crude_odds_ratio")
    return (
        f"- Within projects (Mantel-Haenszel, {rec['strata']} projects with both groups, "
        f"n={fmt.count(rec['n'])}; {rec['strata_dropped']} projects without both groups "
        f"dropped): pooled odds ratio **{rec['pooled_odds_ratio']:.2f}** "
        f"(95% CI {fmt.span(f'{rec["ci95_low"]:.2f}', f'{rec["ci95_high"]:.2f}')}), CMH χ² = "
        f"{rec['cmh_statistic']:,.1f}, p = {fmt.pval(rec['p_value'])} (Holm "
        f"{fmt.pval(rec.get('p_value_holm'))}); crude odds ratio on the same builds "
        f"{fmt.ratio(crude)}; over all labelled builds "
        f"{fmt.ratio(rec.get('crude_odds_ratio_all_projects'))}."
    )


def _change_size(f: Mapping[str, Any]) -> list[str]:
    rows = [[s["label"], *_rate_cells(s)] for s in f["series"]["buckets"]]
    e = f["effects"]
    return [
        *_table(("Total churn (lines)", *_RATE_HEAD), rows),
        f"- {e['comparison']}: risk difference {fmt.pp(_value(e['risk_difference']))} "
        f"(95% CI {fmt.pp_ci(e['risk_difference'])}, Newcombe), risk ratio "
        f"{fmt.ratio(e['risk_ratio'])} (Katz).",
        f"- Monotonic increase across buckets: {'yes' if e['monotonic_increase'] else 'no'}.",
        "",
    ]


def _src_test(f: Mapping[str, Any]) -> list[str]:
    cut = f["src_churn_cutpoints"]
    cells = {c["key"]: c for c in f["series"]["cells"]}
    rows = []
    for q in f["series"]["by_quartile"]:
        no, yes = cells[f"{q['key']}|no_test_change"], cells[f"{q['key']}|tests_changed"]
        within = next(w for w in f["effects"]["within_quartile"] if w["quartile"] == q["key"])
        rd = within["risk_difference"]
        rows.append(
            [
                f"{q['key']} ({q['label']})",
                f"{fmt.pct(no['rate'])} ({fmt.ci(no['ci95_low'], no['ci95_high'])}, "
                f"n={fmt.count(no['n'])})",
                f"{fmt.pct(yes['rate'])} ({fmt.ci(yes['ci95_low'], yes['ci95_high'])}, "
                f"n={fmt.count(yes['n'])})",
                f"{fmt.pp(rd['value']) if rd else 'n/a'} ({fmt.pp_ci(rd)})",
                f"{fmt.pct(q['rate'])} (n={fmt.count(q['n'])})",
            ]
        )
    tests = f["series"]["by_tests_changed"]
    rows.append(
        [
            "all",
            f"{fmt.pct(tests[0]['rate'])} ({fmt.ci(tests[0]['ci95_low'], tests[0]['ci95_high'])}, "
            f"n={fmt.count(tests[0]['n'])})",
            f"{fmt.pct(tests[1]['rate'])} ({fmt.ci(tests[1]['ci95_low'], tests[1]['ci95_high'])}, "
            f"n={fmt.count(tests[1]['n'])})",
            f"{fmt.pp(_value(f['effects']['tests_changed_vs_not']))} "
            f"({fmt.pp_ci(f['effects']['tests_changed_vs_not'])})",
            "",
        ]
    )
    ratio_rows = [
        [s["label"] + ("" if s["in_test"] else " ¹"), *_rate_cells(s)]
        for s in f["series"]["test_change_ratio"]
    ]
    return [
        f"Source-churn cut points (linear interpolation over labelled builds): "
        f"Q1 ≤ {fmt.bound(cut['q25'])} < Q2 ≤ {fmt.bound(cut['q50'])} < Q3 ≤ "
        f"{fmt.bound(cut['q75'])} < Q4 lines; {fmt.pct(cut['share_zero'])} of builds have "
        "src_churn = 0. Failure rate (95% Wilson CI, n) by quartile and test change:",
        "",
        *_table(
            (
                "Src-churn quartile",
                "Test churn = 0",
                "Test churn > 0",
                "Difference (95% CI)",
                "Quartile overall",
            ),
            rows,
            "lrrrr",
        ),
        _mh_line(f["effects"]["tests_changed_within_projects"]).replace(
            "- Within projects", "- Tests changed vs not, within projects"
        ),
        "",
        "Test-change ratio = test churn / (source + test churn):",
        "",
        *_table(("Test-change ratio", *_RATE_HEAD), ratio_rows),
        "¹ Ratio undefined (no line churn): shown for completeness, not part of the ratio test.",
        "",
    ]


def _previous(f: Mapping[str, Any]) -> list[str]:
    s, n = f["leakage_safe"], f["naive"]

    def row(name: str, block: Mapping[str, Any]) -> list[str]:
        pf, pp_ = block["p_fail_given_prev_failed"], block["p_fail_given_prev_passed"]
        rd = block["risk_difference"]
        return [
            name,
            f"{fmt.pct(pf['rate'])} ({fmt.ci(pf['ci95_low'], pf['ci95_high'])}, "
            f"n={fmt.count(pf['n'])})",
            f"{fmt.pct(pp_['rate'])} ({fmt.ci(pp_['ci95_low'], pp_['ci95_high'])}, "
            f"n={fmt.count(pp_['n'])})",
            f"{fmt.pp(_value(rd))} ({fmt.pp_ci(rd)})",
            fmt.ratio(block["risk_ratio"]),
        ]

    timing_rows = []
    for key, label in (
        ("finished_before_start", "predecessor had finished (upper-bound finish < start)"),
        ("overlapping", "predecessor possibly still running (overlap: upper bound ≥ start)"),
        ("unknown_finish", "predecessor finish unknown (no duration)"),
    ):
        b = n["by_overlap"][key]
        rd = b["risk_difference"]
        timing_rows.append(
            [
                label,
                fmt.count(b["n"]),
                f"{fmt.pct(b['p_fail_given_prev_failed']['rate'])} "
                f"(n={fmt.count(b['p_fail_given_prev_failed']['n'])})",
                f"{fmt.pct(b['p_fail_given_prev_passed']['rate'])} "
                f"(n={fmt.count(b['p_fail_given_prev_passed']['n'])})",
                f"{fmt.pp(rd['value']) if rd else 'n/a'} ({fmt.pp_ci(rd)})",
            ]
        )
    ag = f["agreement"]
    ex = f["population"]["excluded"]
    return [
        *_table(
            (
                "Previous-build link",
                "P(fail | prev failed)",
                "P(fail | prev passed)",
                "Difference (95% CI)",
                "Risk ratio (95% CI)",
            ),
            [
                row("leakage-safe: previous FINISHED build", s),
                row("naive: previous STARTED build", n),
            ],
            "lrrrr",
        ),
        f"- Leakage-safe: {s['definition']}. Excluded: "
        f"{fmt.count(ex['leakage_safe_no_finished_previous_build'])} builds with no finished "
        "previous build.",
        f"- Naive: {n['definition']}. Excluded: {fmt.count(ex['naive_first_build_of_project'])} "
        f"first builds and {fmt.count(ex['naive_previous_build_without_outcome'])} builds whose "
        "predecessor was canceled/started.",
        _mh_line(s["within_projects"]).replace(
            "- Within projects", "- Leakage-safe, within projects"
        ),
        _mh_line(n["within_projects"]).replace("- Within projects", "- Naive, within projects"),
        "",
        "**Why the two differ: overlapping builds.** Naive pairs split by whether the naive "
        "predecessor's upper-bound finish (start + summed job time) was before the build's "
        "start. The upper bound overstates the finish, so an overlapping predecessor may in "
        "fact have finished; one that is not known to have finished is what the leakage-safe "
        "rule excludes:",
        "",
        *_table(
            (
                "Naive predecessor",
                "Pairs",
                "P(fail | prev failed)",
                "P(fail | prev passed)",
                "Difference (95% CI)",
            ),
            timing_rows,
        ),
        f"- {fmt.count(n['overlapping_pairs'])} of {fmt.count(n['pairs'])} naive pairs "
        f"({fmt.pct(n['overlapping_share'])}) overlap: the predecessor may still have been "
        "running, so the naive feature can use an outcome that did not exist yet at prediction "
        "time (label leakage). The leakage-safe link skips any predecessor not known to have "
        "finished and takes the build whose (upper-bound) finish is the latest one before the "
        "start.",
        _overlap_reading(s, n),
        f"- The two links give the same previous outcome for "
        f"{fmt.count(ag['same_previous_outcome'])} of {fmt.count(ag['compared'])} builds; they "
        f"differ for {fmt.count(ag['different_previous_outcome'])}.",
        "",
    ]


def _overlap_reading(safe: Mapping[str, Any], naive: Mapping[str, Any]) -> str:
    """Interpretation, worded from the measured direction (never assumed)."""
    ov = naive["by_overlap"]["overlapping"]["risk_difference"]
    fin = naive["by_overlap"]["finished_before_start"]["risk_difference"]
    s_rd, n_rd = safe["risk_difference"], naive["risk_difference"]
    if not (ov and fin and s_rd and n_rd):
        return "- Not enough pairs to compare overlapping and finished predecessors."
    if ov["value"] > fin["value"] and n_rd["value"] > s_rd["value"]:
        return (
            f"- Overlapping pairs agree more strongly ({fmt.pp(ov['value'])} vs "
            f"{fmt.pp(fin['value'])}), and the naive difference exceeds the leakage-safe one "
            f"({fmt.pp(n_rd['value'])} vs {fmt.pp(s_rd['value'])}); on the naive pairs whose "
            f"predecessor had finished the difference is {fmt.pp(fin['value'])}. Overlapping "
            "pairs are builds that ran at about the same time, plausibly on nearly the same code "
            "state (rapid successive pushes, or one commit built as push and PR), whose "
            "predecessor's outcome a predictor may not have had."
        )
    return (
        f"- Overlapping pairs: {fmt.pp(ov['value'])}; finished predecessors: "
        f"{fmt.pp(fin['value'])}; naive overall {fmt.pp(n_rd['value'])} vs leakage-safe "
        f"{fmt.pp(s_rd['value'])}. The overlap does not inflate the naive association here."
    )


def _duration(f: Mapping[str, Any]) -> list[str]:
    g, mw, md = f["groups"], f["mann_whitney"], f["effects"]["median_difference_s"]
    rows = [
        [
            k,
            fmt.count(v["n"]),
            fmt.seconds(v["median_s"]),
            fmt.seconds(v["q25_s"]),
            fmt.seconds(v["q75_s"]),
            fmt.seconds(v["iqr_s"]),
        ]
        for k, v in (("passed", g["passed"]), ("failed (failed + errored)", g["failed"]))
    ] + [
        [
            f"  by status: {s['key']}",
            fmt.count(s["n"]),
            fmt.seconds(s["median_s"]),
            fmt.seconds(s["q25_s"]),
            fmt.seconds(s["q75_s"]),
            fmt.seconds(s["iqr_s"]),
        ]
        for s in f["series"]["by_status"]
    ]
    out = _table(("Group", "Builds", "Median", "Q25", "Q75", "IQR"), rows)
    if mw:
        out += [
            f"- Mann-Whitney U (two-sided, asymptotic) = {fmt.big(mw['u'])} (x = failed, "
            f"y = passed), p = {fmt.pval(mw['p_value'])}.",
            f"- Cliff's δ = **{mw['cliffs_delta']:.3f} ({mw['magnitude']})** = P(failed longer) - "
            "P(passed longer); thresholds 0.147 / 0.33 / 0.474 (Romano et al. 2006).",
            f"- Median difference (failed - passed) {fmt.seconds(md['value'])}, 95% percentile "
            f"bootstrap CI {fmt.seconds(md['ci95_low'])} to {fmt.seconds(md['ci95_high'])} "
            f"({md['n_resamples']} resamples, seed {md['seed']}).",
            f"- Excluded: {fmt.count(f['population']['excluded']['no_duration'])} labelled builds "
            "without a duration.",
            "",
        ]
    return out


def _projects(f: Mapping[str, Any]) -> list[str]:
    d = f["rate_distribution"]
    pop = f["population"]

    def rows(items: Sequence[Mapping[str, Any]]) -> list[list[str]]:
        return [[f"`{p['key']}`", *_rate_cells(p)] for p in items]

    return [
        f"{pop['projects']} projects with ≥ {pop['min_labelled_builds']} labelled builds; "
        f"{fmt.count(pop['excluded']['projects_below_threshold'])} smaller projects "
        f"({fmt.count(pop['excluded']['builds_of_projects_below_threshold'])} builds) are left "
        f"out. Distribution of project failure rates: min {fmt.pct(d['min'])}, Q25 "
        f"{fmt.pct(d['q25'])}, median {fmt.pct(d['median'])}, Q75 {fmt.pct(d['q75'])}, max "
        f"{fmt.pct(d['max'])}.",
        "",
        f"**Top {len(f['series']['top'])} (highest failure rate)**",
        "",
        *_table(("Project", *_RATE_HEAD), rows(f["series"]["top"])),
        f"**Bottom {len(f['series']['bottom'])} (lowest failure rate)**",
        "",
        *_table(("Project", *_RATE_HEAD), rows(f["series"]["bottom"])),
        "- Never passed: "
        + (
            ", ".join(f"`{p['key']}` (n={fmt.count(p['n'])})" for p in f["always_failing"])
            or "none"
        )
        + "; never failed: "
        + (", ".join(f"`{p['key']}`" for p in f["never_failing"]) or "none")
        + ".",
        "",
    ]


def _monthly(f: Mapping[str, Any]) -> list[str]:
    rows = [
        [m["key"] + (" ²" if m["low_volume"] else ""), *_rate_cells(m), fmt.count(m["projects"])]
        for m in f["series"]["months"]
    ]
    return [
        *_table(("Month (UTC)", *_RATE_HEAD, "Active projects"), rows),
        f"² Fewer than {f['population']['low_volume_threshold']} labelled builds: wide interval, "
        "excluded from the chi-square test.",
        "",
    ]


def _two_groups(f: Mapping[str, Any]) -> list[str]:
    rows = [[g["label"], *_rate_cells(g)] for g in f["series"]["groups"]]
    e = f["effects"]
    cross_key = "by_pull_request" if "by_pull_request" in f["series"] else "by_core_team"
    cross = [
        [
            ("pull request" if c["pull_request"] else "push")
            + " · "
            + ("core team" if c["core_team"] else "non-core"),
            *_rate_cells(c),
        ]
        for c in f["series"][cross_key]
    ]
    return [
        *_table(("Group", *_RATE_HEAD), rows),
        f"- {e['comparison']}: risk difference {fmt.pp(_value(e['risk_difference']))} "
        f"(95% CI {fmt.pp_ci(e['risk_difference'])}), risk ratio {fmt.ratio(e['risk_ratio'])}.",
        _mh_line(e["within_projects"]),
        "",
        "PR x core-team cells (context, no test):",
        "",
        *_table(("Cell", *_RATE_HEAD), cross),
    ]


_RENDERERS: dict[str, Callable[[Mapping[str, Any]], list[str]]] = {
    "change_size": _change_size,
    "src_test_churn": _src_test,
    "previous_build": _previous,
    "duration": _duration,
    "projects": _projects,
    "monthly_trend": _monthly,
    "core_team": _two_groups,
    "pull_requests": _two_groups,
}


def _tests_table(report: Mapping[str, Any]) -> list[str]:
    rows = []
    for t in report["chi_square_tests"]["tests"]:
        if t.get("status") != "ok":
            rows.append(
                [
                    f"`{t['key']}`",
                    t["description"],
                    fmt.count(t["n"]),
                    "—",
                    "—",
                    "—",
                    "—",
                    "—",
                    f"not computable: {t.get('reason')}",
                ]
            )
            continue
        rows.append(
            [
                f"`{t['key']}`",
                t["description"],
                fmt.count(t["n"]),
                f"{t['chi2']:,.1f}",
                str(t["dof"]),
                f"{t['cramers_v']:.3f}",
                fmt.pval(t["p_value"]),
                fmt.pval(t["p_value_holm"]),
                t["magnitude"] + (" ³" if t["min_expected_count"] < 5 else ""),
            ]
        )
    return _table(
        ("Test", "Table", "n", "χ²", "dof", "Cramér's V", "p (raw)", "p (Holm)", "V magnitude"),
        rows,
        "llrrrrrrl",
    )


def _stratified_table(report: Mapping[str, Any]) -> list[str]:
    rows = []
    for t in report["stratified_tests"]["tests"]:
        if t.get("status") != "ok":
            rows.append([f"`{t['key']}`", t["description"], fmt.count(t["n"]), *["—"] * 6])
            continue
        rows.append(
            [
                f"`{t['key']}`",
                t["description"],
                fmt.count(t["n"]),
                str(t["strata"]),
                f"{t['pooled_odds_ratio']:.2f} "
                f"({fmt.span(f'{t["ci95_low"]:.2f}', f'{t["ci95_high"]:.2f}')})",
                _or_cell(t.get("crude_odds_ratio")),
                _or_cell(t.get("crude_odds_ratio_all_projects")),
                fmt.pval(t["p_value"]),
                fmt.pval(t["p_value_holm"]),
            ]
        )
    return _table(
        (
            "Test",
            "Comparison",
            "n",
            "Projects",
            "MH pooled OR (95% CI)",
            "Crude OR, same builds (95% CI)",
            "Crude OR, all labelled builds (95% CI)",
            "p (raw)",
            "p (Holm)",
        ),
        rows,
        "llrrrrrrr",
    )


def _or_cell(effect: Mapping[str, Any] | None) -> str:
    if not effect:
        return "n/a"
    return (
        f"{effect['value']:.2f} "
        f"({fmt.span(f'{effect["ci95_low"]:.2f}', f'{effect["ci95_high"]:.2f}')})"
    )


def _headline_table(report: Mapping[str, Any]) -> list[str]:
    rows = []
    for i, f in enumerate(report["findings"], start=1):
        h = f["headline"]
        effect, ci = _effect_cell(h)
        test = "Mann-Whitney U" if h["test"] == "mann_whitney_u" else "Pearson χ²"
        rows.append(
            [
                str(i),
                f"[{f['title']}](#{i}-{_anchor(f['title'])})",
                fmt.count(f["population"]["n"]),
                effect,
                ci,
                h["effect_magnitude"] or "n/a",
                test,
                fmt.pval(h["p_value"]),
                fmt.pval(h["p_value_holm"]),
            ]
        )
    return [
        *_table(
            (
                "#",
                "Finding",
                "n",
                "Effect size",
                "95% CI",
                "Magnitude",
                "Test",
                "p (raw)",
                "p (Holm)",
            ),
            rows,
            "rlrlllrrr",
        ),
        "Magnitude: Cohen's label of the test's Cramér's V (for the duration finding, Romano's "
        "label of Cliff's δ). Holm: adjusted over all chi-square tests of the report.",
        "",
    ]


def _anchor(title: str) -> str:
    keep = "".join(c.lower() if c.isalnum() else ("-" if c in " -" else "") for c in title)
    return keep.strip("-")


def render_markdown(report: Mapping[str, Any], *, generated_on: str) -> str:
    pop, ds, inputs = report["population"], report["dataset"], report["inputs"]
    env, checks = report["environment"], report["checks"]
    lines = [
        "# Historical CI analytics: findings (M3b, spec §7.5)",
        "",
        "> **Generated file, do not edit by hand.** Every number below is in "
        "[`findings.json`](findings.json) (same directory; schema in [`SCHEMA.md`](SCHEMA.md)), "
        "which the web Analytics page reads. Regenerate with the command below.",
        "",
        f"- **Generated on:** {generated_on} (UTC) by `{report['generated_by']}`",
        f"- **Data version:** dataset `{ds['key']}`, md5 `{ds['version']}` ({ds['subset']}). "
        f"Source: {ds['source']}.",
        f"- **dbt run (marts and features):** `{report['run_id']}` from `{inputs['root']}`; "
        f"feature schema `{inputs['feature_schema_version']}`.",
        f"- **Builds:** {fmt.count(pop['builds'])} Java builds in {fmt.count(pop['projects'])} "
        f"projects, {ds['first_started_at']} → {ds['last_started_at']}. Labelled: "
        f"{fmt.count(pop['labelled_builds'])} ({pop['label_rule']}; "
        f"{fmt.count(pop['excluded_without_outcome'])} excluded). Statuses: "
        + ", ".join(f"{k} {fmt.count(v)}" for k, v in pop["status_counts"].items())
        + ".",
        f"- **Overall failure rate:** {fmt.pct(pop['failure']['rate'])} (95% CI "
        f"{fmt.ci(pop['failure']['ci95_low'], pop['failure']['ci95_high'])}, "
        f"{fmt.count(pop['failure']['failures'])} of {fmt.count(pop['failure']['n'])}).",
        "- **Environment:** "
        + ", ".join(f"{k} {v}" for k, v in env.items())
        + f"; report schema {report['schema_version']}.",
        "",
        "## How to read this report",
        "",
        "- Every finding states its **n**, an **effect size** with a **95% confidence "
        "interval** where one exists (Wilson for rates, Newcombe for rate differences, Katz for "
        "risk ratios, percentile bootstrap for the median difference, Robins-Breslow-Greenland "
        "for Mantel-Haenszel odds ratios), the **test** used and its p-value, raw and "
        "Holm-adjusted across the family of tests.",
        "- **Large n:** with 10^4 to 10^5 builds per comparison, differences far too small to "
        "matter reach tiny p-values. Read the effect size (Cramér's V with Cohen's labels: < 0.1 "
        "negligible, 0.1 small, 0.3 medium, 0.5 large for these k x 2 tables), not the p-value.",
        "- **Correlation is not causation.** This is observational data from public projects. "
        "Nothing here says that changing a factor would change the failure rate. The "
        "within-project (Mantel-Haenszel) lines show how much of an association survives "
        "holding the project fixed; some do not.",
        "- p = `< 1e-300` means the tail probability underflows double precision (the test "
        "statistic is enormous), not that p is exactly 0.",
        "",
        "## Headlines",
        "",
        *_headline_table(report),
    ]
    for i, f in enumerate(report["findings"], start=1):
        lines += [
            f"## {i}. {f['title']}",
            "",
            f"*Spec §{f['spec_ref']}. {f['question']}* Population: "
            f"{f['population']['description']} (n = {fmt.count(f['population']['n'])}).",
            "",
            f["summary"],
            "",
            *_RENDERERS[f["key"]](f),
            *_caveats(f),
        ]
    lines += [
        f"## {len(report['findings']) + 1}. All chi-square tests (Holm-adjusted)",
        "",
        f"Pearson χ² tests of independence (no continuity correction, `rl_core.stats.chi_square`), "
        f"{report['chi_square_tests']['computed']} computed, "
        f"{report['chi_square_tests']['not_computable']} not computable. Holm adjustment over this "
        "family with `statsmodels.stats.multitest.multipletests(method='holm')`.",
        "",
        *_tests_table(report),
        "³ Smallest expected cell count below 5: the χ² approximation is unreliable.",
        "",
        "### Within-project tests (Cochran-Mantel-Haenszel, stratified by project)",
        "",
        "A second family, Holm-adjusted separately. Projects in which one of the two groups is "
        "empty carry no within-project information and are dropped. The crude OR on the same "
        "builds differs from the MH OR only by the stratification; the crude OR over all "
        "labelled builds also counts the dropped projects.",
        "",
        *_stratified_table(report),
        "## Input checks",
        "",
        "Cross-checks between the inputs (each value must be 0; the report is not written "
        "otherwise):",
        "",
        *[f"- `{k}`: {v}" for k, v in checks["cross_checks"].items()],
        _naive_check_line(checks["naive_link_vs_int_build_history"]),
        "",
        "Input files:",
        "",
        *_table(
            ("File", "Rows", "Bytes", "sha256"),
            [
                [
                    f"`{v['location']}`",
                    fmt.count(v["rows"]),
                    fmt.count(v["bytes"]),
                    f"`{v['sha256'][:16]}…`",
                ]
                for v in inputs["files"].values()
            ],
            "lrrl",
        ),
        "## Caveats (whole report)",
        "",
        *[f"- {c}" for c in report["caveats"]],
        "",
    ]
    return "\n".join(lines)


def _naive_check_line(check: Mapping[str, Any]) -> str:
    if check.get("status") == "ok":
        return (
            f"- Naive-link mirror vs dbt `int_build_history` (`{check['duckdb_file']}`): "
            f"{fmt.count(check['rows'])} rows compared, {check['missing_rows']} missing, "
            f"{check['mismatched_rows']} mismatched."
        )
    return f"- Naive-link mirror vs dbt `int_build_history`: {check.get('status')} " + (
        f"({check.get('reason')})." if check.get("reason") else "."
    )

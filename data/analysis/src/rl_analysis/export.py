"""Rows for the Postgres analytics tables, derived from a report (no database access here).

The migration (``analytics_runs`` + ``analytics_findings``) and the Airflow task
``export_analytics_to_postgres`` come later; they only have to insert what
:func:`rows_for_postgres` returns. Identifiers are UUIDv5 of the run id (and finding key), so a
re-export of the same run produces the same keys: the load can be an idempotent swap (delete the
run's rows, insert these, in one transaction).

``analytics_runs`` (one row per report run):

==========================  ==========  =========================================================
column                      type        value
==========================  ==========  =========================================================
``id``                      uuid        uuid5(namespace, run_id)
``run_id``                  text        dbt run id of the marts and features (unique)
``report``                  text        report name
``schema_version``          text        findings.json schema version
``dataset_key``             text        e.g. ``travistorrent-5-3-2016``
``dataset_version``         text        md5 of the source file
``feature_schema_version``  text        rl_core feature schema of the feature mart
``first_started_at``        timestamptz first labelled build (ISO 8601 UTC string here)
``last_started_at``         timestamptz last labelled build
``n_builds``                integer     all builds
``n_labelled_builds``       integer     builds with an outcome
``n_failures``              integer     label 1
``failure_rate``            double      overall rate
``failure_rate_ci95_low``   double      Wilson
``failure_rate_ci95_high``  double      Wilson
``generated_by``            text        generation command
``findings_sha256``         text        sha256 of the canonical findings.json bytes
``report_json``             jsonb       the whole report
==========================  ==========  =========================================================

``analytics_findings`` (one row per finding, ``ordinal`` = report order):

========================  ==========  ===========================================================
column                    type        value
========================  ==========  ===========================================================
``id``                    uuid        uuid5(namespace, run_id + "/" + finding_key)
``run_id``                text        FK -> analytics_runs.run_id
``finding_key``           text        stable key, e.g. ``change_size`` (unique per run)
``ordinal``               integer     1-based position
``spec_ref``              text        e.g. ``7.5.1``
``title``                 text
``n``                     integer     builds in the finding's population
``test_name``             text        ``pearson_chi_square`` | ``mann_whitney_u``
``test_key``              text        key in chi_square_tests (NULL for Mann-Whitney)
``p_value``               double
``p_value_holm``          double      NULL when not adjusted (Mann-Whitney)
``effect_name``           text        ``cramers_v`` | ``risk_difference`` | ``cliffs_delta``
``effect_value``          double
``effect_ci95_low``       double      NULL when the effect has no interval
``effect_ci95_high``      double
``effect_magnitude``      text        Cohen (V) / Romano (Cliff's delta) label
``cramers_v``             double      NULL for Mann-Whitney
``summary``               text
``caveats``               jsonb       list of strings
``payload``               jsonb       the full finding (series for charts)
========================  ==========  ===========================================================
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from typing import Any, Final

from rl_analysis.report import dumps

# Fixed namespace for analytics row ids (never change it: ids must stay stable across exports).
NAMESPACE: Final = uuid.uuid5(uuid.NAMESPACE_URL, "urn:releaselens:analytics")


def _uid(*parts: str) -> str:
    return str(uuid.uuid5(NAMESPACE, "/".join(parts)))


def rows_for_postgres(findings: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """``{"analytics_runs": [row], "analytics_findings": [rows]}`` for one report."""
    run_id = str(findings["run_id"])
    pop = findings["population"]
    ds = findings["dataset"]
    run = {
        "id": _uid(run_id),
        "run_id": run_id,
        "report": findings["report"],
        "schema_version": findings["schema_version"],
        "dataset_key": ds["key"],
        "dataset_version": ds["version"],
        "feature_schema_version": findings["inputs"]["feature_schema_version"],
        "first_started_at": ds["first_started_at"],
        "last_started_at": ds["last_started_at"],
        "n_builds": pop["builds"],
        "n_labelled_builds": pop["labelled_builds"],
        "n_failures": pop["failure"]["failures"],
        "failure_rate": pop["failure"]["rate"],
        "failure_rate_ci95_low": pop["failure"]["ci95_low"],
        "failure_rate_ci95_high": pop["failure"]["ci95_high"],
        "generated_by": findings["generated_by"],
        "findings_sha256": hashlib.sha256(dumps(findings)).hexdigest(),
        "report_json": dict(findings),
    }
    rows = []
    for ordinal, f in enumerate(findings["findings"], start=1):
        h = f["headline"]
        rows.append(
            {
                "id": _uid(run_id, f["key"]),
                "run_id": run_id,
                "finding_key": f["key"],
                "ordinal": ordinal,
                "spec_ref": f["spec_ref"],
                "title": f["title"],
                "n": f["population"]["n"],
                "test_name": h["test"],
                "test_key": h["test_key"],
                "p_value": h["p_value"],
                "p_value_holm": h["p_value_holm"],
                "effect_name": h["effect_name"],
                "effect_value": h["effect_value"],
                "effect_ci95_low": h["effect_ci95_low"],
                "effect_ci95_high": h["effect_ci95_high"],
                "effect_magnitude": h["effect_magnitude"],
                "cramers_v": h.get("cramers_v"),
                "summary": f["summary"],
                "caveats": list(f["caveats"]),
                "payload": dict(f),
            }
        )
    keys = [r["finding_key"] for r in rows]
    if len(set(keys)) != len(keys):
        raise ValueError(f"duplicate finding keys: {keys}")
    return {"analytics_runs": [run], "analytics_findings": rows}

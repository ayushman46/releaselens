"""Load the leakage-safe feature mart and assemble model inputs.

The mart (dbt, M3a) lives at
``s3://releaselens-data/features/release_features/schema=<FEATURE_SCHEMA_VERSION>/run_id=<id>/``
with one row per build: ``build_id, project, started_at, label, split`` + every
``ALL_FEATURE_NAMES`` column + ``max_history_input_finished_at``. ``label`` is 1 for
failed/errored and 0 for passed (ADR-0005); canceled/started builds are not in the mart.
``split`` is the global chronological 70/15/15 split.

Every load asserts the schema version, the column contract and the leakage invariant
(``max_history_input_finished_at < started_at`` for every row), so a model can never be trained
on a mart that breaks the history rule.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass, field, replace
from typing import Any, Final

import numpy as np
import pandas as pd
import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from rl_core.features.schema import (
    ALL_FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION,
    FORBIDDEN_INPUTS,
)

DATA_SOURCE: Final = "travistorrent_5_3_2016"
DATASET_MD5: Final = "4713d8b73b667a2c375b89c20f531fc0"
FEATURES_ROOT: Final = "features/release_features"
RAW_JAVA_PREFIX: Final = f"raw/travistorrent/version={DATASET_MD5}/lang=java/"
AUDIT_COLUMN: Final = "max_history_input_finished_at"
SPLITS: Final = ("train", "validation", "test")
ID_COLUMNS: Final = ("build_id", "project", "started_at", "label", "split")
LABELLED_STATUSES: Final = frozenset({"passed", "failed", "errored"})

_DEV_SECRET: Final = "rlens-dev-secret"  # noqa: S105 - documented dev/test-only default


class MartSchemaError(ValueError):
    """The feature mart does not match the contract this code was written against."""


class LeakageError(AssertionError):
    """A feature row uses information that was not available at the build's start."""


# ---------------------------------------------------------------- object store
@dataclass(frozen=True)
class S3Settings:
    endpoint: str = "http://localhost:29000"
    access_key: str = "rlens"
    secret_key: str = _DEV_SECRET
    region: str = "us-east-1"
    bucket_data: str = "releaselens-data"
    bucket_mlflow: str = "releaselens-mlflow"

    @staticmethod
    def from_env() -> S3Settings:
        env = os.environ
        s = S3Settings(
            endpoint=env.get("RL_S3_ENDPOINT", S3Settings.endpoint),
            access_key=env.get("RL_S3_ACCESS_KEY", S3Settings.access_key),
            secret_key=env.get("RL_S3_SECRET_KEY", S3Settings.secret_key),
            region=env.get("RL_S3_REGION", S3Settings.region),
            bucket_data=env.get("RL_S3_BUCKET_DATA", S3Settings.bucket_data),
            bucket_mlflow=env.get("RL_S3_BUCKET_MLFLOW", S3Settings.bucket_mlflow),
        )
        if env.get("RL_ENV") == "prod" and s.secret_key == _DEV_SECRET:
            raise ValueError("RL_S3_SECRET_KEY must be set explicitly when RL_ENV=prod")
        return s


def make_s3_client(settings: S3Settings | None = None) -> Any:
    import boto3
    from botocore.config import Config

    s = settings or S3Settings.from_env()
    return boto3.client(
        "s3",
        endpoint_url=s.endpoint,
        aws_access_key_id=s.access_key,
        aws_secret_access_key=s.secret_key,
        region_name=s.region,
        config=Config(
            s3={"addressing_style": "path"},
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=3,
            read_timeout=60,
        ),
    )


def _list_keys(client: Any, bucket: str, prefix: str) -> list[str]:
    keys: list[str] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = client.list_objects_v2(**kwargs)
        keys.extend(str(o["Key"]) for o in page.get("Contents", []) or [])
        if not page.get("IsTruncated"):
            return sorted(keys)
        token = page.get("NextContinuationToken")


def mart_prefix(run_id: str, schema_version: str = FEATURE_SCHEMA_VERSION) -> str:
    return f"{FEATURES_ROOT}/schema={schema_version}/run_id={run_id}/"


def list_feature_runs(client: Any, bucket: str | None = None) -> list[str]:
    """run_ids that hold at least one Parquet file for the current feature schema version."""
    b = bucket or S3Settings.from_env().bucket_data
    root = f"{FEATURES_ROOT}/schema={FEATURE_SCHEMA_VERSION}/run_id="
    runs = {
        k[len(root) :].split("/", 1)[0]
        for k in _list_keys(client, b, root)
        if k.endswith(".parquet")
    }
    return sorted(runs)


# ---------------------------------------------------------------- the mart
@dataclass(frozen=True)
class Mart:
    """One row per labelled build, sorted by ``(started_at, build_id)``."""

    frame: pd.DataFrame
    run_id: str
    source: str
    dataset_md5: str | None
    feature_schema_version: str = FEATURE_SCHEMA_VERSION
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_synthetic(self) -> bool:
        return self.source != DATA_SOURCE

    @property
    def y(self) -> np.ndarray:
        return np.asarray(self.frame["label"].to_numpy(dtype=np.int64))

    def X(self, features: tuple[str, ...] | list[str]) -> pd.DataFrame:  # noqa: N802
        bad = set(features) & FORBIDDEN_INPUTS
        if bad:
            raise LeakageError(f"forbidden inputs requested as features: {sorted(bad)}")
        return self.frame.loc[:, list(features)].astype(np.float64)

    def split(self, name: str) -> Mart:
        if name not in SPLITS:
            raise ValueError(f"unknown split {name!r}")
        return self.where(self.frame["split"].to_numpy() == name)

    def where(self, mask: np.ndarray) -> Mart:
        return replace(
            self, frame=self.frame.loc[np.asarray(mask, dtype=bool)].reset_index(drop=True)
        )

    @property
    def started_epoch_s(self) -> np.ndarray:
        return _epoch_seconds(self.frame["started_at"])


def _epoch_seconds(col: pd.Series) -> np.ndarray:
    """Datetime (tz-aware or naive UTC) or numeric epoch seconds → float seconds, NaN for null."""
    if pd.api.types.is_datetime64_any_dtype(col.dtype):
        s = col.dt.tz_convert("UTC") if col.dt.tz is not None else col.dt.tz_localize("UTC")
        # unit-agnostic (pandas may hold s/ms/us/ns): timedelta since the epoch in seconds
        secs = (s - pd.Timestamp(0, tz="UTC")) / pd.Timedelta(seconds=1)
        return np.asarray(secs.to_numpy(dtype=np.float64, na_value=np.nan))
    num = pd.to_numeric(col, errors="coerce")
    return np.asarray(num.to_numpy(dtype=np.float64, na_value=np.nan))


def prepare_mart(
    df: pl.DataFrame | pd.DataFrame,
    *,
    run_id: str,
    source: str,
    dataset_md5: str | None,
    schema_version: str = FEATURE_SCHEMA_VERSION,
) -> Mart:
    """Validate the column contract and return a sorted :class:`Mart`."""
    if schema_version != FEATURE_SCHEMA_VERSION:
        raise MartSchemaError(
            f"mart schema {schema_version} != rl_core FEATURE_SCHEMA_VERSION "
            f"{FEATURE_SCHEMA_VERSION}"
        )
    pdf = df.to_pandas() if isinstance(df, pl.DataFrame) else df.copy()
    required = [*ID_COLUMNS, *ALL_FEATURE_NAMES, AUDIT_COLUMN]
    missing = [c for c in required if c not in pdf.columns]
    if missing:
        raise MartSchemaError(f"feature mart is missing columns: {missing}")
    if "feature_schema_version" in pdf.columns:
        versions = set(pdf["feature_schema_version"].dropna().astype(str))
        if versions != {FEATURE_SCHEMA_VERSION}:
            raise MartSchemaError(f"rows carry feature_schema_version {sorted(versions)}")
    if pdf["build_id"].duplicated().any():
        raise MartSchemaError("build_id is not unique")
    if pdf["label"].isna().any() or not set(pdf["label"].unique()) <= {0, 1}:
        raise MartSchemaError("label must be 0/1 with no nulls (canceled/started excluded)")
    if not set(pdf["split"].unique()) <= set(SPLITS):
        raise MartSchemaError(f"split values {sorted(pdf['split'].unique())} not in {SPLITS}")
    if "status" in pdf.columns:
        st = set(pdf["status"].dropna().unique())
        if not st <= LABELLED_STATUSES:
            raise MartSchemaError(
                f"unlabelled statuses in the mart: {sorted(st - LABELLED_STATUSES)}"
            )
        expect = pdf["status"].isin(["failed", "errored"]).astype(int)
        if (expect != pdf["label"].astype(int)).any():
            raise MartSchemaError("label disagrees with status (ADR-0005)")

    if not pd.api.types.is_datetime64_any_dtype(pdf["started_at"].dtype):
        pdf["started_at"] = pd.to_datetime(pdf["started_at"], utc=True)
    elif pdf["started_at"].dt.tz is None:
        pdf["started_at"] = pdf["started_at"].dt.tz_localize("UTC")
    for name in ALL_FEATURE_NAMES:
        pdf[name] = pd.to_numeric(pdf[name], errors="coerce").astype(np.float64)
    pdf["label"] = pdf["label"].astype(np.int64)
    pdf = pdf.sort_values(["started_at", "build_id"], kind="stable").reset_index(drop=True)
    return Mart(pdf, run_id=run_id, source=source, dataset_md5=dataset_md5)


def load_mart(run_id: str, client: Any | None = None, settings: S3Settings | None = None) -> Mart:
    """Read every Parquet file of one mart run from the object store."""
    s = settings or S3Settings.from_env()
    c = client or make_s3_client(s)
    keys = [k for k in _list_keys(c, s.bucket_data, mart_prefix(run_id)) if k.endswith(".parquet")]
    if not keys:
        raise FileNotFoundError(f"no Parquet under s3://{s.bucket_data}/{mart_prefix(run_id)}")
    tables = [
        pq.read_table(io.BytesIO(c.get_object(Bucket=s.bucket_data, Key=k)["Body"].read()))
        for k in keys
    ]
    table = pa.concat_tables(tables, promote_options="default")
    frame = pl.from_arrow(table)
    assert isinstance(frame, pl.DataFrame)
    return prepare_mart(
        frame,
        run_id=run_id,
        source=DATA_SOURCE,
        dataset_md5=DATASET_MD5,
    )


# ---------------------------------------------------------------- label variants (ADR-0005)
def load_raw_status(client: Any | None = None, settings: S3Settings | None = None) -> pd.DataFrame:
    """``build_id → status`` from the raw TravisTorrent 5_3_2016 Java Parquet (one per build)."""
    s = settings or S3Settings.from_env()
    c = client or make_s3_client(s)
    keys = [k for k in _list_keys(c, s.bucket_data, RAW_JAVA_PREFIX) if k.endswith(".parquet")]
    if not keys:
        raise FileNotFoundError(f"no raw Java Parquet under s3://{s.bucket_data}/{RAW_JAVA_PREFIX}")
    parts = []
    for k in keys:
        body = c.get_object(Bucket=s.bucket_data, Key=k)["Body"].read()
        parts.append(pq.read_table(io.BytesIO(body), columns=["tr_build_id", "tr_status"]))
    table = pl.from_arrow(pa.concat_tables(parts))
    assert isinstance(table, pl.DataFrame)
    raw = table.unique()
    if raw["tr_build_id"].n_unique() != raw.height:
        raise MartSchemaError("raw data has builds with more than one status")
    return raw.rename({"tr_build_id": "build_id", "tr_status": "status"}).to_pandas()


def attach_status(mart: Mart, status: pd.DataFrame | None = None) -> Mart:
    """Ensure a ``status`` column (from the mart itself or the raw data, joined on build_id)."""
    if "status" in mart.frame.columns:
        return mart
    st = status if status is not None else load_raw_status()
    merged = mart.frame.merge(st[["build_id", "status"]], on="build_id", how="left")
    if merged["status"].isna().any():
        n = int(merged["status"].isna().sum())
        raise MartSchemaError(f"{n} mart builds have no status in the raw data")
    expect = merged["status"].isin(["failed", "errored"]).astype(int)
    if (expect != merged["label"]).any():
        raise MartSchemaError("mart label disagrees with raw tr_status")
    return replace(mart, frame=merged)


def exclude_errored(mart: Mart) -> Mart:
    """ADR-0005 sensitivity variant: drop ``errored`` builds from every split.

    History features are unchanged (an errored earlier build still counts as a failure in the
    project's history, as it would at serving time); only the rows being predicted change.
    """
    m = attach_status(mart)
    keep = m.frame["status"].to_numpy() != "errored"
    dropped = int((~keep).sum())
    out = m.where(keep)
    return replace(out, notes=(*mart.notes, f"errored builds excluded: {dropped}"))


# ---------------------------------------------------------------- audits and summaries
def leakage_audit(
    mart: Mart, features: tuple[str, ...] | list[str] = ALL_FEATURE_NAMES
) -> dict[str, Any]:
    """Assert the spec section 8.3 invariants on the loaded rows; return the audit numbers."""
    bad = sorted(set(features) & FORBIDDEN_INPUTS)
    if bad:
        raise LeakageError(f"forbidden inputs in the feature list: {bad}")
    started = mart.started_epoch_s
    audit = _epoch_seconds(mart.frame[AUDIT_COLUMN])
    has = ~np.isnan(audit)
    violations = int(np.sum(audit[has] >= started[has]))
    if violations:
        raise LeakageError(
            f"{violations} rows have {AUDIT_COLUMN} >= started_at "
            "(history used an unfinished build)"
        )
    return {
        "rows": len(started),
        "rows_with_history": int(has.sum()),
        "violations": violations,
        "forbidden_in_features": bad,
        "min_gap_seconds": float(np.min(started[has] - audit[has])) if has.any() else None,
    }


def class_balance(mart: Mart) -> dict[str, Any]:
    """Rows, positives and base rate per split (+ status counts when known)."""
    out: dict[str, Any] = {}
    f = mart.frame
    for name in (*SPLITS, "all"):
        part = f if name == "all" else f[f["split"] == name]
        n = len(part)
        pos = int(part["label"].sum())
        entry: dict[str, Any] = {
            "rows": n,
            "positives": pos,
            "base_rate": pos / n if n else None,
            "projects": int(part["project"].nunique()),
        }
        if n:
            entry["started_at_min"] = part["started_at"].min().isoformat()
            entry["started_at_max"] = part["started_at"].max().isoformat()
        if "status" in part.columns:
            entry["status_counts"] = {
                str(k): int(v) for k, v in part["status"].value_counts().sort_index().items()
            }
        out[name] = entry
    return out

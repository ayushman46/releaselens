"""Dataset registry: every TravisTorrent release the pipeline can ingest (ADR-0017).

A :class:`DatasetSpec` bundles everything that differs between releases: the Figshare source
(filename, URL, size, md5), the download sub-directory, the raw schema with per-column cast
patterns, the conversion keys (partition, sort order, unique key), timestamp semantics and the
column catalog used by the data dictionary.

=================================  =========  ============================================
key                                role       file
=================================  =========  ============================================
``travistorrent-5-3-2016``         primary    ``travistorrent_5_3_2016.csv.gz`` (56 cols)
``travistorrent-final-2017-01-25`` secondary  ``final-2017-01-25.csv.gz`` (66 cols)
=================================  =========  ============================================

Every task takes ``{"dataset": <key>}`` (default: the primary). Both land under the same lake /
S3 layout, ``raw/travistorrent/version=<md5>/lang=<lang>/part-00000.parquet`` + ``_manifest.json``;
the md5 differs per file, so the prefixes never collide.

The secondary spec reproduces the pre-registry converter exactly (same SQL semantics, sort key,
schema fingerprint and manifest fields), so its already-published version stays byte-identical
(``manifest_extras=False``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from rl_pipeline.catalog import (
    FINAL_2017_BUILD_INPUT_MAP,
    FINAL_2017_COLUMN_DOCS,
    FINAL_2017_DATAFORMAT,
    FINAL_2017_SPEC_NAME_MAP,
    BuildInputMapping,
    ColumnDoc,
    DataformatDoc,
)
from rl_pipeline.catalog_2016 import (
    TT_5_3_2016_BUILD_INPUT_MAP,
    TT_5_3_2016_COLUMN_DOCS,
    TT_5_3_2016_DATAFORMAT,
    TT_5_3_2016_SPEC_NAME_MAP,
)
from rl_pipeline.dataset import TRAVISTORRENT_5_3_2016, TRAVISTORRENT_FINAL_2017, DatasetSource
from rl_pipeline.schema import (
    FINAL_2017_COLUMNS,
    NULL_TOKENS,
    STRICT_PATTERNS,
    TIMESTAMP_FORMAT,
    TT_5_3_2016_COLUMNS,
    TT_5_3_2016_PATTERNS,
    Columns,
    schema_fingerprint,
)

PRIMARY = "travistorrent-5-3-2016"
SECONDARY = "travistorrent-final-2017-01-25"
DEFAULT_DATASET = PRIMARY


class UnknownDatasetError(ValueError):
    """The requested dataset key is not registered, or contradicts the given version."""


def _frozen(mapping: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(mapping))


@dataclass(frozen=True, eq=False)
class DatasetSpec:
    key: str
    title: str
    source: DatasetSource
    subdir: str  # directory under the datasets root (RL_DATASETS_DIR) holding source.filename
    columns: Columns
    sort_key: tuple[str, ...]
    unique_column: str  # must be non-null and unique: makes sort_key a total order
    started_at_column: str
    timestamp_semantics: str
    # "hash": 'id#id#id' (2017); "bracket": '[id, id, id]' (2016).
    jobs_list_format: Literal["hash", "bracket"]
    column_docs: Mapping[str, ColumnDoc]
    spec_name_map: Mapping[str, str]
    build_input_map: tuple[BuildInputMapping, ...]
    dataformat: DataformatDoc
    dictionary_markdown: str
    dictionary_json: str
    patterns: Mapping[str, str] = field(default_factory=lambda: _frozen({}))
    null_tokens: tuple[str, ...] = NULL_TOKENS
    timestamp_format: str = TIMESTAMP_FORMAT
    partition_column: str = "gh_lang"
    build_id_column: str = "tr_build_id"
    job_id_column: str = "tr_job_id"
    label_column: str = "tr_status"
    duration_column: str = "tr_duration"
    # Upper bound of Parquet bytes per input byte, for the disk/spill budget (measured, + margin).
    output_size_factor: float = 1.0
    # Extra manifest fields (dataset key, key profile, pattern overrides). False only for the
    # secondary release, whose published manifest predates them and must stay byte-identical.
    manifest_extras: bool = True

    def __post_init__(self) -> None:
        names = self.names
        for col in (
            *self.sort_key,
            self.unique_column,
            self.started_at_column,
            self.partition_column,
            self.build_id_column,
            self.job_id_column,
            self.label_column,
            self.duration_column,
        ):
            if col not in names:
                raise ValueError(f"{self.key}: {col!r} is not a column of the schema")
        if self.sort_key[-1] != self.unique_column:
            raise ValueError(f"{self.key}: sort_key must end with the unique column")
        if tuple(self.column_docs) != names:
            raise ValueError(f"{self.key}: column_docs must follow the file order exactly")
        unknown = set(self.patterns) - set(names)
        if unknown:
            raise ValueError(f"{self.key}: patterns for unknown columns {sorted(unknown)}")
        if set(self.spec_name_map.values()) - set(names):
            raise ValueError(f"{self.key}: spec_name_map targets missing from the schema")
        mapped = {m.column for m in self.build_input_map if m.column is not None}
        if mapped - set(names):
            raise ValueError(f"{self.key}: build_input_map columns missing from the schema")

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.columns)

    @property
    def types(self) -> dict[str, str]:
        return dict(self.columns)

    def pattern_for(self, name: str, duck_type: str) -> str | None:
        """Strict full-match pattern for a typed column (override first); None for VARCHAR/BOOL."""
        if name in self.patterns:
            return self.patterns[name]
        return STRICT_PATTERNS.get(duck_type)

    def fingerprint(self) -> str:
        return schema_fingerprint(
            self.columns, self.null_tokens, self.timestamp_format, self.patterns
        )

    def summary(self) -> dict[str, Any]:
        """JSON-serialisable description (CLI ``list_datasets``, DAG checks)."""
        return {
            "key": self.key,
            "title": self.title,
            "default": self.key == DEFAULT_DATASET,
            "filename": self.source.filename,
            "url": self.source.url,
            "md5": self.source.md5,
            "size": self.source.size,
            "subdir": self.subdir,
            "columns": len(self.columns),
            "partition_column": self.partition_column,
            "sort_key": list(self.sort_key),
            "unique_column": self.unique_column,
        }


TT_5_3_2016 = DatasetSpec(
    key=PRIMARY,
    title="TravisTorrent (Figshare `travistorrent_5_3_2016`)",
    source=TRAVISTORRENT_5_3_2016,
    subdir=TRAVISTORRENT_5_3_2016.name,
    columns=TT_5_3_2016_COLUMNS,
    patterns=_frozen(TT_5_3_2016_PATTERNS),
    # tr_job_id is not unique in this release (job rows attached to more than one build), so the
    # dump's own row number completes the order.
    sort_key=("tr_build_id", "tr_job_id", "row"),
    unique_column="row",
    started_at_column="tr_started_at",
    timestamp_semantics=(
        "TIMESTAMP (no zone). tr_started_at is UTC wall-clock (measured: equal to the secondary "
        "archive's explicit-UTC started_at on every joined build). gh_first_commit_created_at is "
        "tr_started_at rendered in Europe/London local time (measured), i.e. not UTC."
    ),
    jobs_list_format="bracket",
    column_docs=_frozen(TT_5_3_2016_COLUMN_DOCS),
    spec_name_map=_frozen(TT_5_3_2016_SPEC_NAME_MAP),
    build_input_map=TT_5_3_2016_BUILD_INPUT_MAP,
    dataformat=TT_5_3_2016_DATAFORMAT,
    dictionary_markdown="DATA_DICTIONARY.md",
    dictionary_json="data_dictionary.json",
    # Measured: 82,276,103 B of Parquet for the 81,902,127 B .csv.gz (factor 1.005).
    output_size_factor=1.25,
)

TT_FINAL_2017 = DatasetSpec(
    key=SECONDARY,
    title="TravisTorrent (Figshare `final-2017-01-25`)",
    source=TRAVISTORRENT_FINAL_2017,
    subdir=TRAVISTORRENT_FINAL_2017.name,
    columns=FINAL_2017_COLUMNS,
    sort_key=("tr_build_id", "tr_job_id"),
    unique_column="tr_job_id",
    started_at_column="gh_build_started_at",
    timestamp_semantics="UTC wall-clock stored as TIMESTAMP (no zone)",
    jobs_list_format="hash",
    column_docs=_frozen(FINAL_2017_COLUMN_DOCS),
    spec_name_map=_frozen(FINAL_2017_SPEC_NAME_MAP),
    build_input_map=FINAL_2017_BUILD_INPUT_MAP,
    dataformat=FINAL_2017_DATAFORMAT,
    dictionary_markdown="DATA_DICTIONARY_final_2017.md",
    dictionary_json="data_dictionary_final_2017.json",
    manifest_extras=False,
)

DATASETS: Mapping[str, DatasetSpec] = MappingProxyType(
    {spec.key: spec for spec in (TT_5_3_2016, TT_FINAL_2017)}
)
assert DEFAULT_DATASET in DATASETS
assert len({s.source.md5 for s in DATASETS.values()}) == len(DATASETS), "md5s must differ"
assert len({s.subdir for s in DATASETS.values()}) == len(DATASETS), "subdirs must differ"


def get_dataset(key: str | None = None) -> DatasetSpec:
    """The spec for ``key`` (default: the primary dataset)."""
    if key is None or key == "":
        return DATASETS[DEFAULT_DATASET]
    if not isinstance(key, str) or key not in DATASETS:
        raise UnknownDatasetError(
            f"unknown dataset {key!r}; registered: {', '.join(sorted(DATASETS))}"
        )
    return DATASETS[key]


def dataset_for_version(version: str | None) -> DatasetSpec | None:
    """The registered spec whose publisher md5 equals ``version``, if any."""
    for spec in DATASETS.values():
        if version and spec.source.md5 == version:
            return spec
    return None


def dataset_for_schema(names: tuple[str, ...]) -> DatasetSpec | None:
    """The registered spec whose column names (in order) equal ``names``, if any."""
    for spec in DATASETS.values():
        if spec.names == tuple(names):
            return spec
    return None


def resolve(params: Mapping[str, Any] | None) -> DatasetSpec:
    """Pick the spec for a task call.

    ``params["dataset"]`` wins; without it, a ``version`` equal to a registered md5 selects that
    release (so a dict from an older run still converts with the right schema); otherwise the
    default (primary). A key that contradicts a registered version is an error.
    """
    params = params or {}
    key = params.get("dataset")
    by_version = dataset_for_version(str(params.get("version") or "") or None)
    if key:
        spec = get_dataset(str(key))
        if by_version is not None and by_version is not spec:
            raise UnknownDatasetError(
                f"dataset {spec.key!r} contradicts version {params.get('version')!r}, which is "
                f"the md5 of {by_version.key!r}"
            )
        return spec
    return by_version or get_dataset(None)

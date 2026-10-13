"""Shared fixtures. All data used here is SYNTHETIC (see tests/fixtures/README.md)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rl_pipeline.convert import DuckDBLimits, convert
from rl_pipeline.datasets import TT_5_3_2016, TT_FINAL_2017, DatasetSpec
from rl_pipeline.hashing import md5_file

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_CSV = FIXTURES / "travistorrent_synthetic_fixture.csv.gz"  # final-2017-01-25 layout
FIXTURE_2016_CSV = FIXTURES / "travistorrent_5_3_2016_synthetic_fixture.csv.gz"
SMALL = DuckDBLimits(memory_limit="256MB", threads=2)


@pytest.fixture
def fixture_csv() -> Path:
    return FIXTURE_CSV


@pytest.fixture
def fixture_version() -> str:
    return md5_file(FIXTURE_CSV)


@pytest.fixture
def fixture_csv_2016() -> Path:
    return FIXTURE_2016_CSV


@pytest.fixture
def fixture_version_2016() -> str:
    return md5_file(FIXTURE_2016_CSV)


@pytest.fixture
def spec_2017() -> DatasetSpec:
    return TT_FINAL_2017


@pytest.fixture
def spec_2016() -> DatasetSpec:
    return TT_5_3_2016


@pytest.fixture
def limits() -> DuckDBLimits:
    return SMALL


@pytest.fixture
def pipeline_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Point every RL_* path at a temp dir so tests never touch the real lake or docs."""
    paths = {
        "lake": tmp_path / "lake",
        "docs": tmp_path / "docs",
        "datasets": tmp_path / "datasets",
    }
    monkeypatch.setenv("RL_LAKE_DIR", str(paths["lake"]))
    monkeypatch.setenv("RL_DOCS_DATA_DIR", str(paths["docs"]))
    monkeypatch.setenv("RL_DATASETS_DIR", str(paths["datasets"]))
    monkeypatch.delenv("RL_DATASET_DIR", raising=False)
    monkeypatch.delenv("RL_DATASET_PATH", raising=False)
    monkeypatch.setenv("RL_DUCKDB_MEMORY_LIMIT", SMALL.memory_limit)
    monkeypatch.setenv("RL_DUCKDB_THREADS", str(SMALL.threads))
    monkeypatch.setenv("RL_ENV", "test")
    return paths


@pytest.fixture(scope="session")
def converted(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """The 2017-layout fixture converted once per session (read-only for tests)."""
    out_root = tmp_path_factory.mktemp("converted")
    return convert(
        FIXTURE_CSV, md5_file(FIXTURE_CSV), out_root, dataset=TT_FINAL_2017, limits=SMALL
    )


@pytest.fixture(scope="session")
def converted_2016(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """The 2016-layout fixture converted once per session (read-only for tests)."""
    out_root = tmp_path_factory.mktemp("converted_2016")
    return convert(
        FIXTURE_2016_CSV, md5_file(FIXTURE_2016_CSV), out_root, dataset=TT_5_3_2016, limits=SMALL
    )


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Any:
    """The CLI reconfigures the root logger; undo that so tests stay isolated."""
    import logging

    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)

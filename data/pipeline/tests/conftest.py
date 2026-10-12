"""Shared fixtures. All data used here is SYNTHETIC (see tests/fixtures/README.md)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rl_pipeline.convert import DuckDBLimits, convert
from rl_pipeline.hashing import md5_file

FIXTURE_CSV = Path(__file__).parent / "fixtures" / "travistorrent_synthetic_fixture.csv.gz"
SMALL = DuckDBLimits(memory_limit="256MB", threads=2)


@pytest.fixture
def fixture_csv() -> Path:
    return FIXTURE_CSV


@pytest.fixture
def fixture_version() -> str:
    return md5_file(FIXTURE_CSV)


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
    monkeypatch.setenv("RL_DATASET_DIR", str(paths["datasets"]))
    monkeypatch.setenv("RL_DUCKDB_MEMORY_LIMIT", SMALL.memory_limit)
    monkeypatch.setenv("RL_DUCKDB_THREADS", str(SMALL.threads))
    monkeypatch.setenv("RL_ENV", "test")
    return paths


@pytest.fixture(scope="session")
def converted(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """The fixture converted once per session (read-only for tests)."""
    out_root = tmp_path_factory.mktemp("converted")
    return convert(FIXTURE_CSV, md5_file(FIXTURE_CSV), out_root, limits=SMALL)


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Any:
    """The CLI reconfigures the root logger; undo that so tests stay isolated."""
    import logging

    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)

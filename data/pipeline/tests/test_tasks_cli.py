"""Task API + CLI contract: JSON-serialisable dicts in/out, result JSON on the last stdout line."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rl_pipeline import tasks
from rl_pipeline.__main__ import main
from rl_pipeline.settings import PipelineSettings


def _last_json(out: str) -> dict[str, Any]:
    lines = [ln for ln in out.splitlines() if ln.strip()]
    data: dict[str, Any] = json.loads(lines[-1])
    return data


def test_task_chain_on_fixture(
    pipeline_env: dict[str, Path], fixture_csv: Path, fixture_version: str
) -> None:
    dataset = {
        "path": str(fixture_csv),
        "md5": fixture_version,
        "size": fixture_csv.stat().st_size,
        "version": fixture_version,
    }
    validated = tasks.validate_raw_schema(dataset)
    assert validated == json.loads(json.dumps(validated))
    conversion = tasks.convert_to_parquet(validated)
    assert conversion == json.loads(json.dumps(conversion))
    assert Path(conversion["local_dir"]).is_relative_to(
        pipeline_env["lake"] / "raw" / "travistorrent"
    )
    assert {"version", "local_dir", "files", "rows_total"} <= set(conversion)
    assert all({"path", "rows", "sha256", "lang"} <= set(f) for f in conversion["files"])
    assert tasks.convert_to_parquet(dataset)["skipped"] is True

    out = tasks.build_data_dictionary({"local_dir": conversion["local_dir"]})
    assert Path(out["markdown_path"]) == pipeline_env["docs"] / "DATA_DICTIONARY.md"
    assert Path(out["json_path"]).is_file()
    assert out == json.loads(json.dumps(out))


def test_convert_rejects_md5_mismatch(pipeline_env: dict[str, Path], fixture_csv: Path) -> None:
    with pytest.raises(Exception, match="md5"):
        tasks.convert_to_parquet({"path": str(fixture_csv), "version": "0" * 32})


def test_locate_missing_dataset_without_download(pipeline_env: dict[str, Path]) -> None:
    with pytest.raises(Exception, match="not found and downloads are disabled"):
        tasks.locate_or_download({"allow_download": False})


def test_cli_prints_result_json_last(
    pipeline_env: dict[str, Path],
    fixture_csv: Path,
    fixture_version: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    params = json.dumps({"path": str(fixture_csv), "version": fixture_version})
    assert main(["validate_raw_schema", "--params-json", params]) == 0
    result = _last_json(capsys.readouterr().out)
    assert result["ok"] is True

    assert main(["convert_to_parquet", "--params-json", params]) == 0
    conv = _last_json(capsys.readouterr().out)
    assert conv["rows_total"] == 30

    params_file = pipeline_env["lake"] / "conv.json"
    params_file.write_text(json.dumps(conv))
    assert main(["build_data_dictionary", "--params-file", str(params_file)]) == 0
    assert Path(_last_json(capsys.readouterr().out)["markdown_path"]).is_file()


def test_cli_failure_exits_nonzero(
    pipeline_env: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["validate_raw_schema", "--params-json", '{"path": "/nope", "version": "x"}']) == 1
    assert capsys.readouterr().out == ""


def test_settings_env_and_prod_guard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RL_DATASET_PATH", str(tmp_path))
    monkeypatch.setenv("RL_S3_ENDPOINT", "http://minio:9000")
    s = PipelineSettings()
    assert s.dataset_dir == tmp_path
    assert s.s3_endpoint == "http://minio:9000"
    assert s.s3_bucket_data == "releaselens-data"
    assert s.raw_travistorrent_dir == s.lake_dir / "raw" / "travistorrent"
    monkeypatch.setenv("RL_ENV", "prod")
    with pytest.raises(ValueError, match="RL_S3_SECRET_KEY"):
        PipelineSettings()
    monkeypatch.setenv("RL_S3_SECRET_KEY", "a-real-secret")
    assert PipelineSettings().env == "prod"


def test_default_paths_point_into_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("RL_DATASET_DIR", "RL_DATASET_PATH", "RL_LAKE_DIR"):
        monkeypatch.delenv(var, raising=False)
    s = PipelineSettings()
    assert s.lake_dir.parts[-2:] == ("data", "lake")
    assert s.dataset_dir.name == "travistorrent-figshare-19314170"


@pytest.mark.parametrize(
    ("value", "expected"), [(True, True), (False, False), ("false", False), (" True ", True)]
)
def test_allow_download_bool_parsing(value: Any, expected: bool) -> None:
    assert tasks._as_bool(value, "allow_download") is expected


@pytest.mark.parametrize("value", ["no", "0", 0, None])
def test_allow_download_rejects_ambiguous_values(value: Any) -> None:
    with pytest.raises(ValueError, match="allow_download"):
        tasks._as_bool(value, "allow_download")

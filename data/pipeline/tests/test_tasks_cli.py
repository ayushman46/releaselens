"""Task API + CLI contract: JSON-serialisable dicts in/out, result JSON on the last stdout line."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rl_pipeline import tasks
from rl_pipeline.__main__ import main
from rl_pipeline.datasets import TT_5_3_2016, TT_FINAL_2017
from rl_pipeline.settings import PipelineSettings

V17 = TT_FINAL_2017.key


def _last_json(out: str) -> dict[str, Any]:
    lines = [ln for ln in out.splitlines() if ln.strip()]
    data: dict[str, Any] = json.loads(lines[-1])
    return data


def test_task_chain_on_fixture(
    pipeline_env: dict[str, Path], fixture_csv: Path, fixture_version: str
) -> None:
    dataset = {
        "dataset": V17,
        "path": str(fixture_csv),
        "md5": fixture_version,
        "size": fixture_csv.stat().st_size,
        "version": fixture_version,
    }
    validated = tasks.validate_raw_schema(dataset)
    assert validated["dataset"] == V17
    assert validated == json.loads(json.dumps(validated))
    conversion = tasks.convert_to_parquet(validated)
    assert conversion == json.loads(json.dumps(conversion))
    assert Path(conversion["local_dir"]).is_relative_to(
        pipeline_env["lake"] / "raw" / "travistorrent"
    )
    assert {"version", "local_dir", "files", "rows_total"} <= set(conversion)
    assert all({"path", "rows", "sha256", "lang"} <= set(f) for f in conversion["files"])
    assert conversion["dataset"] == V17
    assert tasks.convert_to_parquet(dataset)["skipped"] is True

    # the release is recognised from the converted version (schema), no key needed
    out = tasks.build_data_dictionary({"local_dir": conversion["local_dir"]})
    assert out["dataset"] == V17
    assert Path(out["markdown_path"]) == pipeline_env["docs"] / "DATA_DICTIONARY_final_2017.md"
    assert Path(out["json_path"]) == pipeline_env["docs"] / "data_dictionary_final_2017.json"
    assert Path(out["json_path"]).is_file()
    assert out == json.loads(json.dumps(out))


def test_task_chain_defaults_to_primary(
    pipeline_env: dict[str, Path], fixture_csv_2016: Path, fixture_version_2016: str
) -> None:
    dataset = {"path": str(fixture_csv_2016), "version": fixture_version_2016}
    validated = tasks.validate_raw_schema(dataset)
    conversion = tasks.convert_to_parquet(validated)
    assert conversion["dataset"] == TT_5_3_2016.key
    assert conversion["rows_per_lang"] == {"java": 18, "ruby": 8}
    out = tasks.build_data_dictionary(conversion)
    assert out["dataset"] == TT_5_3_2016.key
    assert Path(out["markdown_path"]) == pipeline_env["docs"] / "DATA_DICTIONARY.md"
    assert Path(out["json_path"]) == pipeline_env["docs"] / "data_dictionary.json"


def test_convert_rejects_md5_mismatch(pipeline_env: dict[str, Path], fixture_csv: Path) -> None:
    with pytest.raises(Exception, match="md5"):
        tasks.convert_to_parquet({"path": str(fixture_csv), "version": "0" * 32})


def test_locate_missing_dataset_without_download(pipeline_env: dict[str, Path]) -> None:
    expected = pipeline_env["datasets"] / TT_5_3_2016.subdir / TT_5_3_2016.source.filename
    with pytest.raises(Exception, match="not found and downloads are disabled") as err:
        tasks.locate_or_download({"allow_download": False})
    assert str(expected) in str(err.value)
    expected = pipeline_env["datasets"] / TT_FINAL_2017.subdir / TT_FINAL_2017.source.filename
    with pytest.raises(Exception, match="not found and downloads are disabled") as err:
        tasks.locate_or_download({"dataset": V17, "allow_download": False})
    assert str(expected) in str(err.value)


def test_locate_honours_dataset_dir_override(pipeline_env: dict[str, Path], tmp_path: Path) -> None:
    with pytest.raises(Exception, match="not found") as err:
        tasks.locate_or_download({"dataset_dir": str(tmp_path / "x"), "allow_download": "false"})
    assert str(tmp_path / "x" / TT_5_3_2016.source.filename) in str(err.value)


def test_unknown_dataset_key_fails(pipeline_env: dict[str, Path]) -> None:
    with pytest.raises(ValueError, match="unknown dataset"):
        tasks.locate_or_download({"dataset": "nope", "allow_download": False})


def test_list_datasets_task() -> None:
    out = tasks.list_datasets({})
    assert out["default"] == TT_5_3_2016.key
    assert [d["key"] for d in out["datasets"]] == [TT_5_3_2016.key, V17]
    assert out == json.loads(json.dumps(out))


def test_cli_prints_result_json_last(
    pipeline_env: dict[str, Path],
    fixture_csv: Path,
    fixture_version: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    params = json.dumps({"path": str(fixture_csv), "version": fixture_version})
    assert main(["validate_raw_schema", "--dataset", V17, "--params-json", params]) == 0
    result = _last_json(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["dataset"] == V17

    assert main(["convert_to_parquet", "--dataset", V17, "--params-json", params]) == 0
    conv = _last_json(capsys.readouterr().out)
    assert conv["rows_total"] == 30

    params_file = pipeline_env["lake"] / "conv.json"
    params_file.write_text(json.dumps(conv))
    assert main(["build_data_dictionary", "--params-file", str(params_file)]) == 0
    assert Path(_last_json(capsys.readouterr().out)["markdown_path"]).is_file()


def test_cli_dataset_flag_must_not_contradict_params() -> None:
    params = json.dumps({"dataset": V17})
    with pytest.raises(SystemExit, match="contradicts"):
        main(["list_datasets", "--dataset", TT_5_3_2016.key, "--params-json", params])


def test_cli_failure_exits_nonzero(
    pipeline_env: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["validate_raw_schema", "--params-json", '{"path": "/nope", "version": "x"}']) == 1
    assert capsys.readouterr().out == ""


def test_settings_env_and_prod_guard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("RL_DATASET_DIR", raising=False)
    monkeypatch.delenv("RL_DATASET_PATH", raising=False)
    monkeypatch.setenv("RL_DATASETS_DIR", str(tmp_path))
    monkeypatch.setenv("RL_S3_ENDPOINT", "http://minio:9000")
    s = PipelineSettings()
    assert s.datasets_dir == tmp_path
    assert s.dataset_dir_for("sub") == tmp_path / "sub"
    assert s.s3_endpoint == "http://minio:9000"
    assert s.s3_bucket_data == "releaselens-data"
    assert s.raw_travistorrent_dir == s.lake_dir / "raw" / "travistorrent"
    monkeypatch.setenv("RL_ENV", "prod")
    with pytest.raises(ValueError, match="RL_S3_SECRET_KEY"):
        PipelineSettings()
    monkeypatch.setenv("RL_S3_SECRET_KEY", "a-real-secret")
    assert PipelineSettings().env == "prod"


def test_default_paths_point_into_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("RL_DATASET_DIR", "RL_DATASET_PATH", "RL_DATASETS_DIR", "RL_LAKE_DIR"):
        monkeypatch.delenv(var, raising=False)
    s = PipelineSettings()
    assert s.lake_dir.parts[-2:] == ("data", "lake")
    assert s.datasets_dir.parts[-3:] == ("data", "lake", "downloads")
    assert s.dataset_dir_for(TT_5_3_2016.subdir).name == "travistorrent-figshare-19314170-5_3_2016"


@pytest.mark.parametrize("var", ["RL_DATASET_DIR", "RL_DATASET_PATH"])
def test_removed_single_dataset_vars_fail_loudly(monkeypatch: pytest.MonkeyPatch, var: str) -> None:
    monkeypatch.setenv(var, "/datasets/travistorrent")
    with pytest.raises(ValueError, match="RL_DATASETS_DIR"):
        PipelineSettings()


@pytest.mark.parametrize(
    ("value", "expected"), [(True, True), (False, False), ("false", False), (" True ", True)]
)
def test_allow_download_bool_parsing(value: Any, expected: bool) -> None:
    assert tasks._as_bool(value, "allow_download") is expected


@pytest.mark.parametrize("value", ["no", "0", 0, None])
def test_allow_download_rejects_ambiguous_values(value: Any) -> None:
    with pytest.raises(ValueError, match="allow_download"):
        tasks._as_bool(value, "allow_download")

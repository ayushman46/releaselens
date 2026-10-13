"""Promotion rules (spec section 8.9) against an in-memory fake registry client."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from mlflow.exceptions import MlflowException

from rl_ml import registry
from rl_ml.registry import MODEL_NAME, PromotionRefusedError, promote


@dataclass
class _MV:
    version: str
    tags: dict[str, str]
    run_id: str = "run"


@dataclass
class FakeClient:
    versions: dict[str, _MV] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)

    def get_model_version(self, name: str, version: str) -> _MV:
        return self.versions[str(version)]

    def get_model_version_by_alias(self, name: str, alias: str) -> _MV:
        if alias not in self.aliases:
            raise MlflowException(f"alias {alias} not found")
        return self.versions[self.aliases[alias]]

    def set_registered_model_alias(self, name: str, alias: str, version: str) -> None:
        self.aliases[alias] = str(version)

    def set_model_version_tag(self, name: str, version: str, key: str, value: str) -> None:
        self.versions[str(version)].tags[key] = value


def _tags(pr: float, variant: str = "failed_or_errored", **kw: str) -> dict[str, str]:
    return {
        "test_pr_auc": repr(pr),
        "label_variant": variant,
        "synthetic": "false",
        "leakage_audit": "passed",
    } | kw


def _card(tmp_path: Path, version: str) -> Path:
    p = tmp_path / "MODEL_CARD.md"
    p.write_text(f"# Model card\n\n- **Version:** `{MODEL_NAME}/{version}`\n")
    return p


def _ok() -> tuple[bool, str]:
    return True, "10 passed"


def test_first_model_becomes_champion(tmp_path: Path) -> None:
    c = FakeClient({"1": _MV("1", _tags(0.45))}, {"challenger": "1"})
    d = promote("1", client=c, card_path=_card(tmp_path, "1"), leakage_check=_ok)
    assert d["promoted"]
    assert c.aliases["champion"] == "1"


def test_worse_challenger_is_refused(tmp_path: Path) -> None:
    c = FakeClient({"1": _MV("1", _tags(0.50)), "2": _MV("2", _tags(0.49))}, {"champion": "1"})
    with pytest.raises(PromotionRefusedError, match="champion"):
        promote("2", client=c, card_path=_card(tmp_path, "2"), leakage_check=_ok)
    assert c.aliases["champion"] == "1"


def test_equal_or_better_challenger_is_promoted(tmp_path: Path) -> None:
    c = FakeClient({"1": _MV("1", _tags(0.50)), "2": _MV("2", _tags(0.50))}, {"champion": "1"})
    assert promote("2", client=c, card_path=_card(tmp_path, "2"), leakage_check=_ok)["promoted"]
    assert c.aliases["champion"] == "2"


def test_failing_leakage_tests_block_promotion(tmp_path: Path) -> None:
    c = FakeClient({"1": _MV("1", _tags(0.5))})
    with pytest.raises(PromotionRefusedError, match="test_leakage"):
        promote(
            "1", client=c, card_path=_card(tmp_path, "1"), leakage_check=lambda: (False, "1 failed")
        )
    assert "champion" not in c.aliases


def test_missing_or_stale_model_card_blocks_promotion(tmp_path: Path) -> None:
    c = FakeClient({"2": _MV("2", _tags(0.5))})
    with pytest.raises(PromotionRefusedError, match="MODEL_CARD"):
        promote("2", client=c, card_path=_card(tmp_path, "1"), leakage_check=_ok)
    with pytest.raises(PromotionRefusedError):
        promote("2", client=c, card_path=tmp_path / "missing.md", leakage_check=_ok)


def test_synthetic_or_incomparable_models_are_refused(tmp_path: Path) -> None:
    c = FakeClient({"1": _MV("1", _tags(0.5, synthetic="true"))})
    with pytest.raises(PromotionRefusedError, match="synthetic"):
        promote("1", client=c, card_path=_card(tmp_path, "1"), leakage_check=_ok)
    c = FakeClient(
        {"1": _MV("1", _tags(0.4)), "2": _MV("2", _tags(0.9, variant="errored_excluded"))},
        {"champion": "1"},
    )
    with pytest.raises(PromotionRefusedError, match="not comparable"):
        promote("2", client=c, card_path=_card(tmp_path, "2"), leakage_check=_ok)


def test_run_metrics_flatten_the_results(trained_run) -> None:
    m = registry.run_metrics(trained_run.results)
    assert m["test_pr_auc"] == pytest.approx(trained_run.results["test"]["final"]["pr_auc"])
    assert "test_cal_ece_10_equal_mass" in m
    assert "val_pr_auc" in m
    assert all(isinstance(v, float) for v in m.values())


def test_tags_carry_dataset_and_schema_versions(trained_run) -> None:
    t = registry._tags(trained_run.results)
    assert t["feature_schema_version"] == "1.0.0"
    assert t["synthetic"] == "true"
    assert t["test_set_evaluations"] == "1"
    assert t["leakage_audit"] == "passed"


def test_record_promotion_updates_results_and_card(trained_run, tmp_path: Path) -> None:
    import json

    from rl_ml.data import DATA_SOURCE, DATASET_MD5

    r = json.loads(json.dumps(trained_run.results))
    # fixture only: dress the synthetic run up as a real one so the generator accepts it
    r["data"] |= {"source": DATA_SOURCE, "synthetic": False, "dataset_md5": DATASET_MD5}
    r["registry"] = {
        "run_id": "abc",
        "registered_model": MODEL_NAME,
        "version": "7",
        "alias": "challenger",
    }
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "failed_or_errored.json").write_text(json.dumps(r))
    decision = {
        "model": MODEL_NAME,
        "version": "7",
        "promoted": True,
        "champion_version": None,
        "leakage_tests": {"passed": True, "summary": "12 passed"},
    }
    written = registry.record_promotion(decision, tmp_path)
    assert tmp_path / "MODEL_CARD.md" in written
    assert "promoted to **`champion`**" in (tmp_path / "MODEL_CARD.md").read_text()
    stored = json.loads((tmp_path / "results" / "failed_or_errored.json").read_text())
    assert stored["registry"]["promotion"]["leakage_tests_passed"] is True

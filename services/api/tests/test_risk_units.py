"""Unit tests (no Docker) for M5 serving: model store (cache, checksums, registry fallback),
webhook signatures, the App JWT, Check Run content, the GitHub client, request validation.
Bundles are SYNTHETIC (``rl_api.testing.make_test_bundle``)."""

from __future__ import annotations

import base64
import json
import shutil
import sys
import types
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from pydantic import ValidationError

from rl_api import github_app as gha
from rl_api.risk import modelstore as ms
from rl_api.routers.risk import RiskAssessmentCreate
from rl_api.settings import Settings
from rl_api.testing import make_test_bundle


@pytest.fixture(scope="module")
def bundles(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("unit-bundles")
    return {
        "main": make_test_bundle(root / "main", probability=True),
        "cold": make_test_bundle(root / "cold", cold=True),
    }


# ----------------------------------------------------------------------------- model store
def test_local_bundles_load_with_version_and_checksum(bundles: dict[str, Path]) -> None:
    main = ms.load_local("main", str(bundles["main"]))
    cold = ms.load_local("cold", f"file://{bundles['cold']}")
    assert main.version == "synthetic_main_test/1"
    assert cold.version == "synthetic_cold_test/1"
    assert len(main.checksum) == 64
    assert main.bundle.probability_exposed
    assert not cold.bundle.probability_exposed
    out = main.bundle.assess([{"src_churn": 3}], full_contributions=True)[0]
    assert set(out["contributions"]) == set(main.bundle.features)


def test_a_cold_model_with_history_features_is_refused(bundles: dict[str, Path]) -> None:
    with pytest.raises(ms.ModelLoadError, match="history features"):
        ms.load_local("cold", str(bundles["main"]))
    with pytest.raises(ms.ModelLoadError, match="not a risk bundle"):
        ms.load_local("main", "/nonexistent/bundle")


def test_manifest_detects_tampering(bundles: dict[str, Path], tmp_path: Path) -> None:
    copy = tmp_path / "b"
    shutil.copytree(bundles["cold"], copy)
    digest = ms.write_manifest(copy)
    assert ms.verify_manifest(copy) == digest
    (copy / "metadata.json").write_text((copy / "metadata.json").read_text() + " ")
    assert ms.verify_manifest(copy) is None
    assert ms.verify_manifest(tmp_path / "missing") is None


def test_registry_uris_need_a_tracking_server(tmp_path: Path) -> None:
    with pytest.raises(ms.ModelLoadError, match="RL_MLFLOW_TRACKING_URI"):
        ms.load_model("main", "models:/x@champion", tracking_uri=None, cache_dir=tmp_path)
    with pytest.raises(ms.ModelLoadError, match="unsupported"):
        ms.load_registered("main", "models:/x", tracking_uri="http://m", cache_dir=tmp_path)


class _FakeMlflow:
    """Stands in for the MLflow client: one registered version whose risk_bundle is a dir."""

    def __init__(self, bundle: Path) -> None:
        self.bundle = bundle
        self.down = False
        self.downloads = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = self

        class Client:
            def __init__(self, tracking_uri: str) -> None:
                self.tracking_uri = tracking_uri

            def get_model_version_by_alias(self, name: str, alias: str) -> Any:
                if fake.down:
                    raise ConnectionError("registry unreachable")
                return types.SimpleNamespace(version="7", run_id="r1", tags={})

            def get_model_version(self, name: str, version: str) -> Any:
                return self.get_model_version_by_alias(name, "")

        def download_artifacts(artifact_uri: str, dst_path: str, tracking_uri: str) -> str:
            assert artifact_uri == "runs:/r1/risk_bundle"
            fake.downloads += 1
            target = Path(dst_path) / "risk_bundle"
            shutil.copytree(fake.bundle, target)
            return str(target)

        mlflow = types.ModuleType("mlflow")
        mlflow.MlflowClient = Client  # type: ignore[attr-defined]
        artifacts = types.ModuleType("mlflow.artifacts")
        artifacts.download_artifacts = download_artifacts  # type: ignore[attr-defined]
        mlflow.artifacts = artifacts  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "mlflow", mlflow)
        monkeypatch.setitem(sys.modules, "mlflow.artifacts", artifacts)


def test_registry_load_caches_verifies_and_falls_back(
    bundles: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeMlflow(bundles["main"])
    fake.install(monkeypatch)
    uri = "models:/release_risk_ci@champion"
    first = ms.load_model("main", uri, tracking_uri="http://mlflow", cache_dir=tmp_path)
    assert first.version == "release_risk_ci/7"
    assert (tmp_path / "release_risk_ci" / "7" / "risk_bundle" / ms.MANIFEST).is_file()
    again = ms.load_model("main", uri, tracking_uri="http://mlflow", cache_dir=tmp_path)
    assert fake.downloads == 1  # verified cache, no second download
    assert again.checksum == first.checksum

    fake.down = True  # registry down: the last resolution of the alias is served
    offline = ms.load_model("main", uri, tracking_uri="http://mlflow", cache_dir=tmp_path)
    assert offline.version == "release_risk_ci/7"

    model_file = next((tmp_path / "release_risk_ci" / "7" / "risk_bundle").glob("model.*"))
    model_file.write_bytes(model_file.read_bytes() + b"x")  # corrupt the cache
    with pytest.raises(ms.ModelLoadError, match="cache is invalid"):
        ms.load_model("main", uri, tracking_uri="http://mlflow", cache_dir=tmp_path)
    fake.down = False  # registry back: a bad checksum means download again
    healed = ms.load_model("main", uri, tracking_uri="http://mlflow", cache_dir=tmp_path)
    assert fake.downloads == 2
    assert healed.checksum == first.checksum


async def test_store_start_records_errors_and_never_blocks(bundles: dict[str, Path]) -> None:
    store = ms.ModelStore(
        uris={"main": str(bundles["main"]), "cold": "/nope"},
        tracking_uri=None,
        cache_dir=Path("/tmp/unused"),
        retry_interval_s=3600,
    )
    await store.start(wait_s=30)
    assert store.main is not None
    assert store.cold is None
    assert "cold" in store.errors
    assert store.pending() == ["cold"]
    await store.stop()
    empty = ms.ModelStore(uris={"main": None, "cold": None}, tracking_uri=None, cache_dir=Path())
    await empty.start()
    assert empty.status()["loaded"] == {}


# ----------------------------------------------------------------------------- GitHub App
def test_webhook_signature() -> None:
    body = b'{"zen":"hi"}'
    good = gha.compute_signature("s3cret", body)
    assert good.startswith("sha256=")
    assert gha.verify_signature("s3cret", body, good)
    assert not gha.verify_signature("other", body, good)
    assert not gha.verify_signature("s3cret", body + b" ", good)
    assert not gha.verify_signature("s3cret", body, None)
    assert not gha.verify_signature("s3cret", body, good.removeprefix("sha256="))
    assert not gha.verify_signature("s3cret", body, "sha256=ü")  # non-ASCII never raises


def _pem(key: Any, tmp_path: Path) -> Path:
    path = tmp_path / "key.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return path


def test_app_jwt_is_rs256_with_short_expiry(tmp_path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    loaded = gha.load_private_key(_pem(key, tmp_path))
    token = gha.app_jwt("12345", loaded, now=1_800_000_000)
    head, payload, sig = token.split(".")

    def dec(s: str) -> bytes:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    key.public_key().verify(
        dec(sig), f"{head}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )
    claims = json.loads(dec(payload))
    assert claims == {"iat": 1_800_000_000 - 60, "exp": 1_800_000_000 + 540, "iss": "12345"}
    ec_key = ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(gha.GitHubAppError, match="RSA"):
        gha.load_private_key(_pem(ec_key, tmp_path))


ASSESSMENT = {
    "assessment_id": "0192aaaa-0000-7000-8000-000000000001",
    "model_version": "release_risk_ci/3",
    "risk_score": 78,
    "risk_band": "HIGH",
    "probability": 0.61,
    "top_increasing": [
        {
            "feature": "log1p_src_churn",
            "label": "Source lines changed",
            "value": 1200.0,
            "contribution": 0.41,
            "text": "Source lines changed is in the top 10% | of builds",
        }
    ],
    "top_reducing": [],
    "history": {"finished_builds_used": 12, "cold_start": False},
    "details_url": "https://app.releaselens.dev/app/risk/0192",
}


def test_check_run_payload() -> None:
    p = gha.check_run_payload(ASSESSMENT, name="ReleaseLens risk", head_sha="a" * 40)
    assert p["output"]["title"] == "ReleaseLens: Risk HIGH, 78/100"
    assert p["conclusion"] == "neutral"
    assert p["status"] == "completed"
    assert p["details_url"] == ASSESSMENT["details_url"]
    summary = p["output"]["summary"]
    assert "not a production outage probability" in summary
    assert "12 finished builds" in summary
    assert "top 10% \\| of builds" in summary  # table-safe
    assert "+0.41" in summary
    assert "61%" in summary
    cold = ASSESSMENT | {
        "history": {"finished_builds_used": 0, "cold_start": True},
        "details_url": "http://localhost/x",
        "probability": None,
    }
    p2 = gha.check_run_payload(cold, name="n", head_sha="b" * 40)
    assert "details_url" not in p2  # GitHub accepts only https
    assert "without build history" in p2["output"]["summary"]


async def test_client_caches_installation_tokens() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "t1", "expires_at": "2099-01-01T00:00:00Z"})
        if request.url.path == "/repos/acme/app/check-runs":
            assert request.headers["authorization"] == "Bearer t1"
            return httpx.Response(201, json={"id": 1})
        return httpx.Response(403, json={})

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = gha.GitHubAppClient(app_id="1", private_key=key, http=http)
        await client.create_check_run(5, "acme/app", {"name": "x"})
        await client.create_check_run(5, "acme/app", {"name": "y"})
        with pytest.raises(gha.GitHubAppError, match="403"):
            await client.create_check_run(5, "acme/other", {"name": "z"})
    assert calls.count("POST /app/installations/5/access_tokens") == 1


# ----------------------------------------------------------------------------- validation
BODY = {
    "repository": "acme/payments",
    "commit_sha": "a" * 40,
    "event": "push",
    "triggered_at": "2026-10-13T02:00:00Z",
    "change_facts": {"src_churn": 1, "by_core_team_member": True, "sloc": None},
}


def test_request_validation() -> None:
    ok = RiskAssessmentCreate.model_validate(BODY)
    assert ok.change_facts["by_core_team_member"] is True
    for bad in (
        {"change_facts": {"src_churn": 1, "gh_src_churn": 2}},
        {"change_facts": {"src_churn": "1"}},
        {"change_facts": {"src_churn": float("inf")}},
        {"triggered_at": "2026-10-13T02:00:00"},
        {"commit_sha": "g" * 40},
        {"pull_request": 0},
        {"branch": "a\nb"},
        {"ci_run_url": "javascript:alert(1)"},
        {"unexpected": 1},
    ):
        with pytest.raises(ValidationError):
            RiskAssessmentCreate.model_validate(BODY | bad)
    sha256_repo = RiskAssessmentCreate.model_validate(BODY | {"commit_sha": "A" * 64})
    assert sha256_repo.commit_sha == "A" * 64  # lower-cased by the handler


def test_settings_validation() -> None:
    with pytest.raises(ValidationError, match="numeric"):
        Settings(env="test", github_app_id="my-app")
    s = Settings(env="test", github_app_id="42", github_app_private_key_file="/k.pem")
    assert s.github_app_configured
    assert s.rate_policy("risk").name == "risk"
    assert not Settings(env="test").github_app_configured


def test_the_github_action_request_matches_the_api_contract() -> None:
    """The Action's extractor (integrations/github-action) is the API's client: every fact it
    sends must be a known fact, and its request shape must validate (with nulls)."""
    import importlib.util

    path = (
        Path(__file__).resolve().parents[3] / "integrations/github-action/extract_change_facts.py"
    )
    spec = importlib.util.spec_from_file_location("rl_extract_change_facts", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    from rl_core.features.change import BUILD_INPUT_FIELDS

    assert set(module.FACT_ORDER) <= set(BUILD_INPUT_FIELDS)
    body = {
        "repository": "acme/payments",
        "commit_sha": "a" * 40,
        "pull_request": None,
        "branch": None,
        "event": "push",
        "triggered_at": "2026-10-13T02:00:00Z",
        "ci_run_url": "https://github.com/acme/payments/actions/runs/1",
        "previous_built_sha": None,
        "extractor": {"name": module.EXTRACTOR_NAME, "version": module.EXTRACTOR_VERSION},
        "change_facts": dict.fromkeys(module.FACT_ORDER),
    }
    assert RiskAssessmentCreate.model_validate(body).change_facts["src_churn"] is None

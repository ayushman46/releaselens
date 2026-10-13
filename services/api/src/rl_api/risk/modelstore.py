"""The risk models the API serves, loaded once per process (spec §9, ADR-0012).

Two roles:

* ``main``: the champion (``RL_MODEL_URI``, default ``models:/release_risk_ci@champion``);
* ``cold``: a model trained **without** history features (``RL_COLD_MODEL_URI``, default
  ``models:/release_risk_ci_cold@champion``), used when a repository has no finished builds.

A ``models:/<name>@<alias>`` or ``models:/<name>/<version>`` URI is resolved in the MLflow
registry at ``RL_MLFLOW_TRACKING_URI``. The version's ``risk_bundle`` artifact is downloaded once
into ``RL_MODEL_CACHE_DIR/<name>/<version>/`` together with a SHA-256 manifest; later starts
verify the manifest and reuse the files (a mismatch re-downloads). If the registry is
unreachable at startup, the last resolved version cached for that alias is used. A plain
directory (or ``file://`` URI) is loaded as is: the tests and air-gapped installs use that.

Nothing here runs per request: requests read the already-loaded bundles. If a model cannot be
loaded, the store keeps retrying in the background (``RL_MODEL_RETRY_INTERVAL_S``) and the
assessment endpoint answers ``503 no_model_loaded``; a score is never made up.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

from rl_core.features.schema import ALL_FEATURE_NAMES, HISTORY_FEATURES

if TYPE_CHECKING:
    from rl_ml.predictor import RiskModelBundle

log = logging.getLogger("rl_api.risk.models")

Role = Literal["main", "cold"]
MANIFEST: Final = "checksums.json"
SOURCE: Final = "source.json"
_MODELS_URI = re.compile(
    r"^models:/(?P<name>[A-Za-z0-9_.-]+)(?:@(?P<alias>[A-Za-z0-9_-]+)|/(?P<version>\d+))$"
)


class ModelLoadError(RuntimeError):
    pass


@dataclass(frozen=True)
class LoadedModel:
    role: Role
    name: str  # registered model name (or the bundle directory name)
    version: str  # what the API reports, e.g. "release_risk_ci/3"
    uri: str
    path: Path
    checksum: str  # SHA-256 over the bundle manifest
    bundle: RiskModelBundle
    loaded_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def summary(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "model_version": self.version,
            "uri": self.uri,
            "checksum": self.checksum,
            "features": len(self.bundle.features),
            "probability_exposed": self.bundle.probability_exposed,
            "loaded_at": self.loaded_at.isoformat(),
        }


# ---------------------------------------------------------------- checksums
def file_checksums(directory: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(directory.iterdir()):
        if p.is_file() and p.name not in (MANIFEST, SOURCE):
            out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def manifest_digest(files: dict[str, str]) -> str:
    text = "".join(f"{name}:{digest}\n" for name, digest in sorted(files.items()))
    return hashlib.sha256(text.encode()).hexdigest()


def write_manifest(directory: Path) -> str:
    files = file_checksums(directory)
    digest = manifest_digest(files)
    (directory / MANIFEST).write_text(json.dumps({"files": files, "digest": digest}, indent=1))
    return digest


def verify_manifest(directory: Path) -> str | None:
    """The digest if the directory matches its manifest, else None."""
    try:
        recorded = json.loads((directory / MANIFEST).read_text())
    except (OSError, ValueError):
        return None
    files = file_checksums(directory)
    if files != recorded.get("files") or not files:
        return None
    digest = manifest_digest(files)
    return digest if digest == recorded.get("digest") else None


# ---------------------------------------------------------------- loading
def _check_bundle(bundle: RiskModelBundle, role: Role) -> None:
    unknown = set(bundle.features) - set(ALL_FEATURE_NAMES)
    if unknown:
        raise ModelLoadError(f"bundle uses features rl_core does not compute: {sorted(unknown)}")
    if role == "cold":
        leaking = set(bundle.features) & set(HISTORY_FEATURES)
        if leaking:
            raise ModelLoadError(
                f"a cold-start model must not use history features: {sorted(leaking)}"
            )


def _load_bundle(path: Path) -> RiskModelBundle:
    from rl_ml.predictor import RiskModelBundle

    bundle = RiskModelBundle.load(path)
    est = getattr(bundle.model, "estimator", None)
    if est is not None and hasattr(est, "set_params") and hasattr(est, "get_booster"):
        # One row per request, and requests already run concurrently: no OpenMP fan-out
        # (docs/api/RISK_ASSESSMENTS.md "Latency").
        est.set_params(n_jobs=1)
        est.get_booster().set_param({"nthread": 1})
    return bundle


def load_local(role: Role, uri: str) -> LoadedModel:
    path = Path(uri.removeprefix("file://")).expanduser()
    if not (path / "metadata.json").is_file():
        raise ModelLoadError(f"{path} is not a risk bundle (no metadata.json)")
    digest = manifest_digest(file_checksums(path))
    bundle = _load_bundle(path)
    _check_bundle(bundle, role)
    meta_version = bundle.meta.get("registered_version")
    version = str(meta_version) if meta_version else f"local:{path.name}@{digest[:12]}"
    return LoadedModel(role, path.name, version, uri, path, digest, bundle)


def _alias_pointer(cache: Path, name: str, alias: str) -> Path:
    return cache / name / f"alias-{alias}.json"


def _bounded_mlflow_http() -> None:
    """MLflow retries a dead server for minutes by default; fail fast instead (the store
    retries on its own schedule). Explicit environment settings win."""
    os.environ.setdefault("MLFLOW_HTTP_REQUEST_MAX_RETRIES", "1")
    os.environ.setdefault("MLFLOW_HTTP_REQUEST_BACKOFF_FACTOR", "1")
    os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", "15")


def load_registered(role: Role, uri: str, *, tracking_uri: str, cache_dir: Path) -> LoadedModel:
    m = _MODELS_URI.match(uri)
    if m is None:
        raise ModelLoadError(f"unsupported model URI {uri!r}")
    name, alias, version = m.group("name"), m.group("alias"), m.group("version")
    cache = cache_dir.expanduser()
    _bounded_mlflow_http()
    try:
        from mlflow import MlflowClient

        client = MlflowClient(tracking_uri=tracking_uri)
        mv = (
            client.get_model_version_by_alias(name, alias)
            if alias
            else client.get_model_version(name, str(version))
        )
        version = str(mv.version)
        run_id = str(mv.run_id)
        bundle_uri = dict(mv.tags or {}).get("risk_bundle_uri") or f"runs:/{run_id}/risk_bundle"
    except Exception as exc:  # registry down: fall back to the last resolution of this alias
        pointer = _alias_pointer(cache, name, alias) if alias else None
        if pointer is None or not pointer.is_file():
            raise ModelLoadError(f"cannot resolve {uri} in MLflow: {exc}") from exc
        version = str(json.loads(pointer.read_text())["version"])
        target = cache / name / version / "risk_bundle"
        digest = verify_manifest(target)
        if digest is None:
            raise ModelLoadError(f"cannot resolve {uri} and the cache is invalid") from exc
        log.warning(
            "model registry unreachable; serving the cached version",
            extra={"uri": uri, "version": version, "error": str(exc)},
        )
        bundle = _load_bundle(target)
        _check_bundle(bundle, role)
        return LoadedModel(role, name, f"{name}/{version}", uri, target, digest, bundle)

    target = cache / name / version / "risk_bundle"
    digest = verify_manifest(target) if target.is_dir() else None
    if digest is None:
        if target.exists():
            log.warning("cached bundle failed its checksum; downloading again", extra={"uri": uri})
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        import mlflow.artifacts

        with tempfile.TemporaryDirectory(dir=target.parent, prefix=".download-") as tmp:
            got = Path(
                mlflow.artifacts.download_artifacts(
                    artifact_uri=bundle_uri, dst_path=tmp, tracking_uri=tracking_uri
                )
            )
            if not (got / "metadata.json").is_file():
                raise ModelLoadError(f"{bundle_uri} has no metadata.json")
            write_manifest(got)
            (got / SOURCE).write_text(
                json.dumps(
                    {"uri": uri, "name": name, "version": version, "bundle_uri": bundle_uri},
                    indent=1,
                )
            )
            got.rename(target)
        digest = verify_manifest(target)
        if digest is None:
            raise ModelLoadError("downloaded bundle does not match its own manifest")
    if alias:
        _alias_pointer(cache, name, alias).write_text(json.dumps({"version": version}))
    bundle = _load_bundle(target)
    _check_bundle(bundle, role)
    return LoadedModel(role, name, f"{name}/{version}", uri, target, digest, bundle)


def load_model(role: Role, uri: str, *, tracking_uri: str | None, cache_dir: Path) -> LoadedModel:
    if uri.startswith("models:/"):
        if not tracking_uri:
            raise ModelLoadError(f"{uri} needs RL_MLFLOW_TRACKING_URI")
        return load_registered(role, uri, tracking_uri=tracking_uri, cache_dir=cache_dir)
    return load_local(role, uri)


# ---------------------------------------------------------------- the store
@dataclass
class ModelStore:
    uris: dict[Role, str | None]
    tracking_uri: str | None
    cache_dir: Path
    retry_interval_s: float = 60.0
    models: dict[Role, LoadedModel] = field(default_factory=dict)
    errors: dict[Role, str] = field(default_factory=dict)
    _task: asyncio.Task[None] | None = field(default=None, repr=False)
    _first_attempt: asyncio.Future[None] | None = field(default=None, repr=False)

    @property
    def main(self) -> LoadedModel | None:
        return self.models.get("main")

    @property
    def cold(self) -> LoadedModel | None:
        return self.models.get("cold")

    def pending(self) -> list[Role]:
        return [r for r, uri in self.uris.items() if uri and r not in self.models]

    def load_pending(self) -> None:
        """Try every configured, not yet loaded model (blocking; run it in a thread)."""
        for role in self.pending():
            uri = self.uris[role]
            assert uri is not None
            try:
                model = load_model(
                    role, uri, tracking_uri=self.tracking_uri, cache_dir=self.cache_dir
                )
            except Exception as exc:
                self.errors[role] = str(exc)[:500]
                log.warning(
                    "risk model not loaded", extra={"role": role, "uri": uri, "error": str(exc)}
                )
                continue
            self.models[role] = model
            self.errors.pop(role, None)
            log.info("risk model loaded", extra=model.summary())

    async def start(self, *, wait_s: float = 30.0) -> None:
        """Load in the background; wait up to ``wait_s`` for the first attempt so a healthy
        registry is ready before traffic, but never let a slow one hold up startup."""
        if not self.pending():
            return
        self._task = asyncio.create_task(self._run(), name="risk-model-loader")
        if wait_s > 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(self._first), timeout=wait_s)

    @property
    def _first(self) -> asyncio.Future[None]:
        if self._first_attempt is None:
            self._first_attempt = asyncio.get_running_loop().create_future()
        return self._first_attempt

    async def _run(self) -> None:
        first = self._first
        try:
            await asyncio.to_thread(self.load_pending)
        finally:
            if not first.done():
                first.set_result(None)
        while self.pending() and self.retry_interval_s > 0:
            await asyncio.sleep(self.retry_interval_s)
            await asyncio.to_thread(self.load_pending)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def status(self) -> dict[str, Any]:
        return {
            "loaded": {role: m.summary() for role, m in self.models.items()},
            "errors": dict(self.errors),
            "configured": {role: bool(uri) for role, uri in self.uris.items()},
        }

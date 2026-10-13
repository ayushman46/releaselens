"""GitHub App: webhook signatures and Check Runs (BLOCKERS.md B1, docs/integrations/GITHUB_APP.md).

* :func:`verify_signature`: ``X-Hub-Signature-256`` is ``sha256=<hex HMAC-SHA256 of the raw
  body>`` keyed with the webhook secret; compared in constant time.
* :class:`GitHubAppClient`: signs an RS256 JWT with the App's private key (issuer = App id,
  valid 9 minutes, backdated 60 s for clock skew), exchanges it for an installation access token
  (cached until shortly before it expires) and creates Check Runs. It uses the API's shared
  ``httpx.AsyncClient`` (no redirects, bounded timeouts), so tests substitute an
  ``httpx.MockTransport`` and never reach GitHub.

The Check Run is advisory: its conclusion is always ``neutral``. The model card rules out
automatically blocking merges on the score; teams that want a gate use the Action's
``fail-on-band``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

log = logging.getLogger("rl_api.github_app")

SIGNATURE_HEADER: Final = "x-hub-signature-256"
EVENT_HEADER: Final = "x-github-event"
DELIVERY_HEADER: Final = "x-github-delivery"
API_VERSION: Final = "2022-11-28"
JWT_TTL_S: Final = 9 * 60  # GitHub allows at most 10 minutes
JWT_BACKDATE_S: Final = 60
TOKEN_REFRESH_MARGIN_S: Final = 120
DISCLAIMER: Final = (
    "This is a CI-failure risk estimate from historical patterns, not a production outage "
    "probability. The listed signals describe how the model arrived at its score (SHAP "
    "contributions), not the causes of a failure."
)
MAX_SUMMARY_CHARS: Final = 60_000  # GitHub's limit is 65,535


class GitHubAppError(RuntimeError):
    pass


# ---------------------------------------------------------------- webhooks
def compute_signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = compute_signature(secret, body)
    return hmac.compare_digest(expected.encode("ascii"), header.strip().encode("utf-8", "replace"))


# ---------------------------------------------------------------- JWT (RS256)
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def app_jwt(app_id: str, private_key: rsa.RSAPrivateKey, *, now: float | None = None) -> str:
    issued = int(now if now is not None else time.time()) - JWT_BACKDATE_S
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {"iat": issued, "exp": issued + JWT_BACKDATE_S + JWT_TTL_S, "iss": app_id}
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    )
    signature = private_key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64url(signature)}"


def load_private_key(path: str | Path) -> rsa.RSAPrivateKey:
    key = serialization.load_pem_private_key(Path(path).expanduser().read_bytes(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise GitHubAppError("the GitHub App private key must be an RSA key (PEM)")
    return key


# ---------------------------------------------------------------- Check Run content
def _fmt_value(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return str(value)


def _md(text: Any, limit: int = 200) -> str:
    """One line of untrusted text, safe inside a Markdown table cell."""
    s = " ".join(str(text).split())[:limit]
    return s.replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;")


def check_run_title(band: str, score: int) -> str:
    return f"ReleaseLens: Risk {band}, {score}/100"


def check_run_summary(assessment: dict[str, Any]) -> str:
    """Markdown summary: score, band, top signals both ways, history and the disclaimer."""
    hist = assessment.get("history") or {}
    lines = [
        f"**CI-failure risk: {assessment['risk_band']}** (score {assessment['risk_score']}/100, "
        "the percentile among the model's validation builds).",
        "",
    ]
    if assessment.get("probability") is not None:
        lines += [f"Calibrated failure probability: {assessment['probability']:.0%}.", ""]
    if hist.get("cold_start"):
        lines += [
            "_No finished builds of this repository are known to ReleaseLens yet: scored by the "
            "model without build history (lower accuracy)._",
            "",
        ]
    else:
        lines += [f"Build history used: {hist.get('finished_builds_used', 0)} finished builds.", ""]
    for title, key in (("Raising the score", "top_increasing"), ("Lowering it", "top_reducing")):
        items = assessment.get(key) or []
        lines.append(f"**{title}**")
        lines.append("")
        if not items:
            lines += ["- none", ""]
            continue
        lines += ["| signal | value | contribution |", "|---|---:|---:|"]
        lines += [
            f"| {_md(i.get('text') or i.get('label'))} | {_md(_fmt_value(i.get('value')), 40)} "
            f"| {float(i.get('contribution', 0.0)):+.2f} |"
            for i in items
        ]
        lines.append("")
    lines += [
        f"Model `{_md(assessment['model_version'], 80)}`.",
        "",
        f"> {DISCLAIMER}",
    ]
    if assessment.get("details_url"):
        lines += ["", f"[Open the assessment in ReleaseLens]({assessment['details_url']})"]
    return "\n".join(lines)[:MAX_SUMMARY_CHARS]


def check_run_payload(assessment: dict[str, Any], *, name: str, head_sha: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": name,
        "head_sha": head_sha,
        "status": "completed",
        "conclusion": "neutral",
        "external_id": str(assessment["assessment_id"]),
        "output": {
            "title": check_run_title(assessment["risk_band"], int(assessment["risk_score"])),
            "summary": check_run_summary(assessment),
        },
    }
    url = assessment.get("details_url")
    if isinstance(url, str) and url.startswith("https://"):
        payload["details_url"] = url  # GitHub accepts only absolute https URLs here
    return payload


# ---------------------------------------------------------------- client
@dataclass
class _Token:
    value: str
    expires_at: float


@dataclass
class GitHubAppClient:
    app_id: str
    private_key: rsa.RSAPrivateKey = field(repr=False)
    http: httpx.AsyncClient = field(repr=False)
    api_url: str = "https://api.github.com"
    _tokens: dict[int, _Token] = field(default_factory=dict, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def _headers(self, auth: str) -> dict[str, str]:
        return {
            "Authorization": auth,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "releaselens-github-app",
        }

    def _url(self, path: str) -> str:
        return f"{self.api_url.rstrip('/')}{path}"

    async def installation_token(self, installation_id: int) -> str:
        async with self._lock:
            cached = self._tokens.get(installation_id)
            if cached is not None and cached.expires_at - TOKEN_REFRESH_MARGIN_S > time.time():
                return cached.value
            jwt = app_jwt(self.app_id, self.private_key)
            resp = await self.http.post(
                self._url(f"/app/installations/{installation_id}/access_tokens"),
                headers=self._headers(f"Bearer {jwt}"),
            )
            if resp.status_code != 201:
                raise GitHubAppError(f"installation token request failed: HTTP {resp.status_code}")
            body = resp.json()
            expires = _parse_github_time(body.get("expires_at")) or time.time() + 3000
            self._tokens[installation_id] = _Token(str(body["token"]), expires)
            return str(body["token"])

    async def create_check_run(
        self, installation_id: int, repository: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        owner, _, repo = repository.partition("/")
        token = await self.installation_token(installation_id)
        resp = await self.http.post(
            self._url(f"/repos/{owner}/{repo}/check-runs"),
            headers=self._headers(f"Bearer {token}"),
            json=payload,
        )
        if resp.status_code == 401:  # token revoked early: drop it so the next call refreshes
            self._tokens.pop(installation_id, None)
        if resp.status_code != 201:
            raise GitHubAppError(f"check run creation failed: HTTP {resp.status_code}")
        out: dict[str, Any] = resp.json()
        return out


def _parse_github_time(value: Any) -> float | None:
    from datetime import datetime

    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None

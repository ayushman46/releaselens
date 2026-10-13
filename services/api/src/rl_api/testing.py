"""Test helpers (not imported by production code).

OTLP (M7): build OTLP trace requests from rl_core spans. Spans are grouped into one
``ResourceSpans`` per ``(service, version, environment)``, the shape an OpenTelemetry SDK +
Collector produce.

SaaS (M10):

* :func:`saas_settings`: test settings (cheap Argon2, generous rate limits, no Kafka).
* :class:`Harness`: the app wired to a recording mailer, a fake Stripe gateway and a
  GitHub ``httpx.MockTransport``, run with its real lifespan inside the test's event loop.
* :class:`UserClient`: an ``httpx.AsyncClient`` with its own cookie jar that adds the
  CSRF header on mutating requests, like the web app does.

The fake gateways never pretend to be Stripe or GitHub outside tests: they are only
reachable by passing them to :func:`rl_api.app.create_app` explicitly.
"""

from __future__ import annotations

import base64
import gzip
import json
import uuid
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import FastAPI
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, Status
from opentelemetry.proto.trace.v1.trace_pb2 import Span as OtlpSpan

from rl_api.app import create_app
from rl_api.mail import RecordingMailer
from rl_api.settings import Settings
from rl_core.telemetry.model import Span, SpanKind, StatusCode

_KIND = {
    SpanKind.INTERNAL: OtlpSpan.SPAN_KIND_INTERNAL,
    SpanKind.SERVER: OtlpSpan.SPAN_KIND_SERVER,
    SpanKind.CLIENT: OtlpSpan.SPAN_KIND_CLIENT,
    SpanKind.PRODUCER: OtlpSpan.SPAN_KIND_PRODUCER,
    SpanKind.CONSUMER: OtlpSpan.SPAN_KIND_CONSUMER,
}
_STATUS = {
    StatusCode.UNSET: Status.STATUS_CODE_UNSET,
    StatusCode.OK: Status.STATUS_CODE_OK,
    StatusCode.ERROR: Status.STATUS_CODE_ERROR,
}


def any_value(value: Any) -> AnyValue:
    if isinstance(value, bool):
        return AnyValue(bool_value=value)
    if isinstance(value, int):
        return AnyValue(int_value=value)
    if isinstance(value, float):
        return AnyValue(double_value=value)
    if isinstance(value, list | tuple):
        out = AnyValue()
        out.array_value.values.extend(any_value(v) for v in value)
        return out
    return AnyValue(string_value=str(value))


def key_values(attrs: Mapping[str, Any]) -> list[KeyValue]:
    return [KeyValue(key=k, value=any_value(v)) for k, v in attrs.items()]


def to_otlp(
    spans: Iterable[Span], *, extra_resource: Mapping[str, Any] | None = None
) -> ExportTraceServiceRequest:
    groups: dict[tuple[str, str | None, str | None], list[Span]] = {}
    for span in spans:
        groups.setdefault((span.service, span.service_version, span.environment), []).append(span)
    request = ExportTraceServiceRequest()
    for (service, version, environment), members in groups.items():
        resource: dict[str, Any] = {"service.name": service}
        if version is not None:
            resource["service.version"] = version
        if environment is not None:
            resource["deployment.environment"] = environment
        resource.update(extra_resource or {})
        rs = ResourceSpans()
        rs.resource.attributes.extend(key_values(resource))
        scope = rs.scope_spans.add()
        scope.scope.name = "rl-tests"
        for span in members:
            attrs: dict[str, Any] = dict(span.attributes)
            if span.http_route is not None:
                attrs["http.route"] = span.http_route
            if span.http_status is not None:
                attrs["http.response.status_code"] = span.http_status
            out = scope.spans.add()
            out.trace_id = bytes.fromhex(span.trace_id)
            out.span_id = bytes.fromhex(span.span_id)
            if span.parent_span_id is not None:
                out.parent_span_id = bytes.fromhex(span.parent_span_id)
            out.name = span.name
            out.kind = _KIND[span.kind]
            out.start_time_unix_nano = span.start_ns
            out.end_time_unix_nano = span.end_ns
            out.status.code = _STATUS[span.status_code]
            out.attributes.extend(key_values(attrs))
        request.resource_spans.append(rs)
    return request


def to_otlp_json(request: ExportTraceServiceRequest) -> dict[str, Any]:
    """OTLP/JSON: protobuf JSON mapping with hex trace/span ids and integer enums."""
    from google.protobuf import json_format

    obj: dict[str, Any] = json_format.MessageToDict(request, use_integers_for_enums=True)
    for rs in obj.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for span in ss.get("spans", []):
                for name in ("traceId", "spanId", "parentSpanId"):
                    if name in span:
                        span[name] = base64.b64decode(span[name]).hex()
    return obj


def protobuf_body(spans: Iterable[Span], *, compress: bool = False, **kw: Any) -> bytes:
    data = to_otlp(spans, **kw).SerializeToString()
    return gzip.compress(data) if compress else data


def json_body(spans: Iterable[Span], *, compress: bool = False, **kw: Any) -> bytes:
    data = json.dumps(to_otlp_json(to_otlp(spans, **kw))).encode()
    return gzip.compress(data) if compress else data


# ----------------------------------------------------------------------------- SaaS (M10)
TEST_PASSWORD = "Granite-Lantern-Orbit-47"  # noqa: S105 - a test fixture value gitleaks:allow
TEST_BASE_URL = "https://testserver"


def saas_settings(database_url: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "env": "test",
        "database_url": database_url,
        "kafka_bootstrap": "127.0.0.1:1",  # unreachable on purpose: these tests need no broker
        "kafka_delivery_timeout_s": 1.0,
        "otel_enabled": False,
        "log_level": "WARNING",
        # Cheapest legal Argon2id: the parameters are covered by their own tests.
        "argon2_time_cost": 1,
        "argon2_memory_kib": 1024,
        "argon2_parallelism": 1,
        "rate_limit_auth_burst": 100_000,
        "rate_limit_auth_per_minute": 100_000.0,
        "rate_limit_login_account_burst": 100_000,
        "rate_limit_login_account_per_minute": 100_000.0,
        "rate_limit_ingest_burst": 100_000,
        "rate_limit_ingest_per_minute": 100_000.0,
        "rate_limit_risk_burst": 100_000,
        "rate_limit_risk_per_minute": 100_000.0,
        # No model registry in these tests; risk tests point these at a local bundle.
        "model_uri": None,
        "cold_model_uri": None,
        "web_base_url": "https://app.releaselens.test",
    }
    values.update(overrides)
    return Settings(**values)


@dataclass
class FakeBillingGateway:
    """Records Stripe calls instead of making them."""

    checkouts: list[dict[str, Any]] = field(default_factory=list)
    portals: list[dict[str, Any]] = field(default_factory=list)

    async def create_checkout_session(self, **kwargs: Any) -> str:
        self.checkouts.append(kwargs)
        return f"https://checkout.stripe.test/c/{len(self.checkouts)}"

    async def create_portal_session(self, **kwargs: Any) -> str:
        self.portals.append(kwargs)
        return f"https://billing.stripe.test/p/{len(self.portals)}"


@dataclass
class FakeGitHub:
    """Answers GitHub's OAuth token endpoint and the two identity endpoints."""

    user_id: int = 4242
    login: str = "octo"
    name: str | None = "Octo Cat"
    emails: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {"email": "octo@example.com", "primary": True, "verified": True, "visibility": None}
        ]
    )
    token_error: str | None = None
    requests: list[httpx.Request] = field(default_factory=list)
    token_status: int = 201
    check_run_status: int = 201
    check_runs: list[dict[str, Any]] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "github.com" and request.url.path == "/login/oauth/access_token":
            if self.token_error:
                return httpx.Response(200, json={"error": self.token_error})
            return httpx.Response(200, json={"access_token": "gho_test", "token_type": "bearer"})
        if request.url.host == "api.github.com" and request.url.path == "/user":
            return httpx.Response(
                200, json={"id": self.user_id, "login": self.login, "name": self.name}
            )
        if request.url.host == "api.github.com" and request.url.path == "/user/emails":
            return httpx.Response(200, json=self.emails)
        # GitHub App (M5): installation tokens and Check Runs.
        path = request.url.path
        if (
            request.method == "POST"
            and path.startswith("/app/installations/")
            and path.endswith("/access_tokens")
        ):
            return httpx.Response(
                self.token_status,
                json={"token": "ghs_installation_test", "expires_at": "2099-01-01T00:00:00Z"},
            )
        if request.method == "POST" and path.endswith("/check-runs"):
            self.check_runs.append(json.loads(request.content))
            return httpx.Response(self.check_run_status, json={"id": len(self.check_runs)})
        return httpx.Response(404, json={"message": "Not Found"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


class UserClient(httpx.AsyncClient):
    """One browser: its own cookies, the CSRF header on unsafe methods."""

    csrf: str | None = None
    user_id: uuid.UUID | None = None
    email: str | None = None

    async def request(self, method: str, url: Any, **kwargs: Any) -> httpx.Response:
        if method.upper() not in ("GET", "HEAD", "OPTIONS") and self.csrf:
            headers = dict(kwargs.pop("headers", None) or {})
            headers.setdefault("X-CSRF-Token", self.csrf)
            kwargs["headers"] = headers
        return await super().request(method, url, **kwargs)


@dataclass
class Harness:
    settings: Settings
    mailer: RecordingMailer = field(default_factory=RecordingMailer)
    gateway: FakeBillingGateway | None = field(default_factory=FakeBillingGateway)
    github: FakeGitHub = field(default_factory=FakeGitHub)
    app: FastAPI = field(init=False)

    def __post_init__(self) -> None:
        self.app = create_app(
            self.settings,
            mailer=self.mailer,
            http_transport=self.github.transport(),
            billing_gateway=self.gateway,
        )

    @asynccontextmanager
    async def running(self) -> AsyncIterator[Harness]:
        async with self.app.router.lifespan_context(self.app):
            yield self

    def client(
        self, *, peer: str = "127.0.0.1", cls: Callable[..., UserClient] = UserClient
    ) -> UserClient:
        transport = httpx.ASGITransport(app=self.app, client=(peer, 50000))
        return cls(transport=transport, base_url=TEST_BASE_URL)

    def token_from_mail(self, tag: str, to: str) -> str:
        text = self.mailer.last(tag, to).text
        return text.split("#token=", 1)[1].split()[0]

    async def signup(self, email: str, password: str = TEST_PASSWORD) -> UserClient:
        """Sign up, verify through the e-mailed link, sign in."""
        client = self.client()
        resp = await client.post("/api/v1/auth/signup", json={"email": email, "password": password})
        assert resp.status_code == 202, resp.text
        token = self.token_from_mail("verify_email", email)
        resp = await client.post("/api/v1/auth/verify-email", json={"token": token})
        assert resp.status_code == 200, resp.text
        return await self.login(email, password, client=client)

    async def login(
        self, email: str, password: str = TEST_PASSWORD, *, client: UserClient | None = None
    ) -> UserClient:
        client = client or self.client()
        resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        client.csrf = body["csrf_token"]
        client.user_id = uuid.UUID(body["user"]["id"])
        client.email = email
        return client

    async def create_org(self, owner: UserClient, slug: str | None = None) -> uuid.UUID:
        slug = slug or f"org-{uuid.uuid4().hex[:10]}"
        resp = await owner.post("/api/v1/orgs", json={"name": slug.title(), "slug": slug})
        assert resp.status_code == 201, resp.text
        return uuid.UUID(resp.json()["id"])

    async def add_member(
        self, owner: UserClient, org_id: uuid.UUID, role: str, email: str | None = None
    ) -> UserClient:
        """Invite through the API, sign the invitee up, accept the e-mailed invitation."""
        email = email or f"{role}-{uuid.uuid4().hex[:8]}@example.com"
        resp = await owner.post(
            f"/api/v1/orgs/{org_id}/invites", json={"email": email, "role": role}
        )
        assert resp.status_code == 201, resp.text
        invite_token = self.token_from_mail("invitation", email)
        member = await self.signup(email)
        resp = await member.post("/api/v1/invites/accept", json={"token": invite_token})
        assert resp.status_code == 200, resp.text
        return member


def cookie_header(**cookies: str | None) -> dict[str, str]:
    """An explicit ``Cookie`` header (for replaying a captured or forged cookie from a
    client whose own jar is empty)."""
    return {"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items() if v is not None)}


def unique_email(prefix: str = "user") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}@example.com"


def stripe_signature(payload: bytes, secret: str, timestamp: int) -> str:
    """``Stripe-Signature`` for ``payload``, via the Stripe SDK's own signing helper."""
    import stripe

    header: str = stripe.WebhookSignature.generate_signature_header(
        payload.decode("utf-8"), secret, timestamp=timestamp
    )
    return header


def stripe_event(
    event_type: str, obj: dict[str, Any], *, created: int, event_id: str | None = None
) -> bytes:
    return json.dumps(
        {
            "id": event_id or f"evt_{uuid.uuid4().hex[:24]}",
            "object": "event",
            "api_version": "2026-08-26.dahlia",
            "created": created,
            "type": event_type,
            "livemode": False,
            "data": {"object": obj},
        }
    ).encode()


# ----------------------------------------------------------------------------- risk (M5)
def make_test_bundle(
    directory: Any,
    *,
    cold: bool = False,
    cuts: tuple[float, float, float] | None = None,
    probability: bool = False,
    version: str | None = None,
    seed: int = 0,
) -> Any:
    """SYNTHETIC: a tiny XGBoost risk bundle for API tests. Never a real model, never a result.

    ``cold`` drops every history feature (the cold-start role). ``cuts`` overrides the
    (MEDIUM, HIGH, CRITICAL) raw-score cut-points, e.g. ``(0, 0, 2)`` makes every score HIGH.
    ``probability`` marks the calibration gate as passed (a Platt calibrator is stored).
    """
    from pathlib import Path

    from rl_core.features.schema import FEATURE_NAMES, HISTORY_FEATURES
    from rl_ml.bands import BandCuts, ScoreScale
    from rl_ml.calibrate import fit_platt
    from rl_ml.data import prepare_mart
    from rl_ml.explain import BackgroundStats
    from rl_ml.models import XGBModel, _xgb
    from rl_ml.monitoring import training_bins
    from rl_ml.predictor import RiskModelBundle
    from rl_ml.synthetic import SYNTHETIC_SOURCE, make_synthetic_mart

    mart = prepare_mart(
        make_synthetic_mart(n_projects=6, builds_per_project=(40, 80), seed=seed),
        run_id="synthetic-api-test",
        source=SYNTHETIC_SOURCE,
        dataset_md5=None,
    )
    train, val = mart.split("train"), mart.split("validation")
    feats = tuple(
        f
        for f in FEATURE_NAMES
        if not bool(train.frame[f].isna().all()) and not (cold and f in HISTORY_FEATURES)
    )
    est = _xgb(
        {"max_depth": 3, "learning_rate": 0.2}, spw=1.0, seed=seed, n_estimators=25, early=None
    )
    est.fit(train.X(feats), train.y)
    model = XGBModel(est)
    vs = model.score(val.X(feats))
    bundle = RiskModelBundle(
        model=model,
        features=feats,
        scale=ScoreScale.fit(vs),
        cuts=BandCuts(*cuts) if cuts else BandCuts.fit(val.y, vs),
        calibrator=fit_platt(vs, val.y) if probability else None,
        calibration={"method": "platt", "passed": probability},
        stats=BackgroundStats.fit(train.X(feats)),
        psi_bins=training_bins(train.X(feats)),
        meta={
            "synthetic": True,
            "registered_version": version
            or ("synthetic_cold_test/1" if cold else "synthetic_main_test/1"),
        },
    )
    path = Path(directory)
    bundle.save(path)
    return path

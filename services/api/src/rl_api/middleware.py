"""Pure-ASGI middleware (streaming-safe): request ids and access logging, security
headers, and request body size limits."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Iterable

from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from rl_core.obs import bind_log_context, reset_log_context

log = logging.getLogger("rl_api.access")

_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_QUIET_PATHS = frozenset({"/healthz", "/readyz"})


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        token = bind_log_context(request_id=request_id)
        start = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            path = scope.get("path", "")
            if path not in _QUIET_PATHS or status_code >= 400:
                log.info(
                    "request",
                    extra={
                        "method": scope.get("method"),
                        "path": path,
                        "status": status_code,
                        "duration_ms": round((time.perf_counter() - start) * 1000, 2),
                    },
                )
            reset_log_context(token)


# ----------------------------------------------------------------------------- security headers
# JSON APIs render nothing, so the policy allows nothing. The interactive docs (dev/test
# only) need Swagger UI's script, style and image from its CDN.
API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
DOCS_CSP = (
    "default-src 'none'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data: https://fastapi.tiangolo.com; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'"
)
_DOCS_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc"})


class SecurityHeadersMiddleware:
    """Adds hardening headers to every HTTP response (unless the route set its own)."""

    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        self.app = app
        self.hsts = hsts

    def _headers(self, path: str) -> list[tuple[bytes, bytes]]:
        headers = [
            (b"content-security-policy", (DOCS_CSP if path in _DOCS_PATHS else API_CSP).encode()),
            (b"x-content-type-options", b"nosniff"),
            (b"referrer-policy", b"no-referrer"),
            (b"x-frame-options", b"DENY"),
            (b"cross-origin-opener-policy", b"same-origin"),
            (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
        ]
        if self.hsts:
            headers.append((b"strict-transport-security", b"max-age=63072000; includeSubDomains"))
        return headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        extra = self._headers(scope.get("path", ""))

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                headers.extend((k, v) for k, v in extra if k not in present)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


# ----------------------------------------------------------------------------- body size limit
class _BodyTooLargeError(StarletteHTTPException):
    """Raised from ``receive`` mid-stream. It is an HTTPException so that FastAPI's body
    parsing re-raises it unchanged and the JSON error handler renders a 413."""

    def __init__(self, limit: int) -> None:
        super().__init__(413, f"request body exceeds {limit} bytes")


class BodySizeLimitMiddleware:
    """Rejects request bodies over ``max_bytes`` with 413, whether the client declared the
    size (``Content-Length``) or streams it chunked. Large bodies are refused before they
    are buffered, so a client cannot make the API hold an arbitrarily large payload."""

    def __init__(
        self, app: ASGIApp, *, max_bytes: int, overrides: Iterable[tuple[str, int]] = ()
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.overrides = tuple(overrides)  # (path prefix, limit), e.g. OTLP ingest

    def _limit(self, path: str) -> int:
        for prefix, limit in self.overrides:
            if path.startswith(prefix):
                return limit
        return self.max_bytes

    @staticmethod
    async def _reject(scope: Scope, send: Send, limit: int) -> None:
        request_id = scope.get("state", {}).get("request_id")
        body = json.dumps(
            {
                "error": {
                    "code": "payload_too_large",
                    "message": f"request body exceeds {limit} bytes",
                    "request_id": request_id,
                }
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self._limit(scope.get("path", ""))
        declared = dict(scope.get("headers", [])).get(b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > limit
            except ValueError:
                too_big = True  # a garbled length is not worth guessing about
            if too_big:
                await self._reject(scope, send, limit)
                return

        received = 0
        started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLargeError(limit)
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLargeError:
            if not started:
                await self._reject(scope, send, limit)

"""GitHub App webhook: ``POST /api/v1/integrations/github/webhook`` (BLOCKERS.md B1).

1. Without ``RL_GITHUB_WEBHOOK_SECRET``: ``503 github_app_not_configured``.
2. ``X-Hub-Signature-256`` must be the HMAC-SHA256 of the raw body with that secret (constant-time
   comparison) or the answer is ``401 invalid_signature``. Nothing is parsed before that.
3. ``X-GitHub-Delivery`` is recorded once (``webhook_deliveries``); a redelivery answers
   ``200 {"status": "duplicate"}`` and changes nothing.
4. ``installation.id`` → tenant (``github_installations``, linked by an operator). An unknown or
   suspended installation is acknowledged and ignored: GitHub only needs a 2xx.
5. The event is applied in the tenant's context (:mod:`rl_api.risk.github_events`), in the same
   transaction as the delivery record.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from rl_api.errors import ApiError
from rl_api.github_app import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
    verify_signature,
)
from rl_api.risk.github_events import MalformedPayloadError, handle_event
from rl_api.settings import Settings
from rl_core.obs import bind_log_context
from rl_core.tenancy import Principal, TenantContext
from rl_db.admin.github import find_installation, record_delivery, set_delivery_outcome

log = logging.getLogger("rl_api.github_webhook")

router = APIRouter(prefix="/api/v1/integrations/github", tags=["integrations"])

_EVENT_RE = re.compile(r"^[a-z_]{1,64}$")
_DELIVERY_RE = re.compile(r"^[A-Za-z0-9-]{1,100}$")


def _reply(status: int, body: dict[str, Any]) -> JSONResponse:
    return JSONResponse(body, status_code=status)


@router.post(
    "/webhook",
    status_code=202,
    summary="GitHub App webhook (signed with RL_GITHUB_WEBHOOK_SECRET)",
    responses={
        200: {"description": "ping, or a redelivery that was already processed"},
        202: {"description": "Accepted (processed or deliberately ignored)"},
        400: {"description": "Missing GitHub headers or a body that is not a JSON object"},
        401: {"description": "Missing or invalid X-Hub-Signature-256"},
        503: {"description": "The GitHub App is not configured (github_app_not_configured)"},
    },
)
async def github_webhook(request: Request) -> JSONResponse:
    settings: Settings = request.app.state.settings
    if settings.github_webhook_secret is None:
        raise ApiError(503, "github_app_not_configured", "the GitHub App is not configured")
    body = await request.body()
    if not verify_signature(
        settings.github_webhook_secret.get_secret_value(),
        body,
        request.headers.get(SIGNATURE_HEADER),
    ):
        log.info("github webhook rejected", extra={"reason": "bad_signature"})
        raise ApiError(401, "invalid_signature", "missing or invalid X-Hub-Signature-256")

    event = request.headers.get(EVENT_HEADER, "")
    delivery = request.headers.get(DELIVERY_HEADER, "")
    if not _EVENT_RE.fullmatch(event) or not _DELIVERY_RE.fullmatch(delivery):
        raise ApiError(
            400, "bad_request", "missing or malformed X-GitHub-Event / X-GitHub-Delivery"
        )
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise ApiError(400, "bad_request", "body is not JSON") from exc
    if not isinstance(payload, dict):
        raise ApiError(400, "bad_request", "body is not a JSON object")
    if event == "ping":
        return _reply(200, {"status": "pong"})

    raw_inst = payload.get("installation", {})
    inst_id = raw_inst.get("id") if isinstance(raw_inst, dict) else None
    installation_id = (
        inst_id if isinstance(inst_id, int) and not isinstance(inst_id, bool) else None
    )
    bind_log_context(github_delivery=delivery, github_event=event)

    async with request.app.state.db_sessions.begin() as session:
        fresh = await record_delivery(
            session, delivery_id=delivery, event=event, installation_id=installation_id
        )
        if not fresh:
            return _reply(200, {"status": "duplicate"})
        installation = (
            await find_installation(session, installation_id)
            if installation_id is not None
            else None
        )
        if installation is None:
            outcome = "ignored:unknown_installation"
            await set_delivery_outcome(session, delivery, outcome=outcome, tenant_id=None)
            return _reply(202, {"status": "ignored", "outcome": outcome})
        if installation.suspended_at is not None and event != "installation":
            outcome = "ignored:installation_suspended"
            await set_delivery_outcome(
                session, delivery, outcome=outcome, tenant_id=installation.tenant_id
            )
            return _reply(202, {"status": "ignored", "outcome": outcome})
        ctx = TenantContext(tenant_id=installation.tenant_id, principal=Principal.SYSTEM)
        bind_log_context(tenant_id=str(ctx.tenant_id))
        try:
            async with session.begin_nested():
                outcome = await handle_event(
                    session, ctx, event, payload, app_id=settings.github_app_id
                )
        except MalformedPayloadError as exc:
            outcome = f"ignored:malformed:{exc}"[:64]
        await set_delivery_outcome(session, delivery, outcome=outcome, tenant_id=ctx.tenant_id)
    status = "ignored" if outcome.startswith("ignored") else "processed"
    log.info("github webhook handled", extra={"outcome": outcome})
    return _reply(202, {"status": status, "outcome": outcome})

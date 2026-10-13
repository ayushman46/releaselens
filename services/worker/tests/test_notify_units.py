"""Notification rendering and the Slack channel's failure classification (no Docker)."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from rl_worker.notify import render
from rl_worker.notify.channels import DeliveryError, SlackSender

PAYLOAD = {
    "deployment_id": str(uuid.uuid4()),
    "analysis_id": str(uuid.uuid4()),
    "kind": "final",
    "service": "payment",
    "environment": "production",
    "version": "v2",
    "previous_version": "v1",
    "status": "REGRESSED",
    "severity_score": 58.64,
    "severity_band": "HIGH",
    "probable_origin": "payment",
    "confidence": "HIGH",
    "affected_services": ["orders", "gateway"],
    "engine_version": "regression-engine/1.0.0",
    "completed_at": "2026-10-13T02:10:00Z",
}


def test_regression_message_wording() -> None:
    title = render.headline("regression", PAYLOAD)
    assert (
        title
        == "Regression HIGH (severity 58.6/100) after deploying payment v2 (from v1) to production"
    )
    lines = render.detail_lines("regression", PAYLOAD)
    assert "Probable origin: payment (confidence HIGH)" in lines
    assert "Affected services: orders, gateway" in lines
    text = " ".join(lines).lower()
    assert "root cause" not in text
    assert "probability" not in text
    no_origin = render.detail_lines(
        "regression", {**PAYLOAD, "probable_origin": None, "confidence": "LOW"}
    )
    assert "Probable origin: not determined (confidence LOW)" in no_origin


def test_slack_payload_escapes_customer_strings_and_links_to_the_app() -> None:
    hostile = {**PAYLOAD, "service": "<!channel> <https://evil.example|click> & co"}
    msg = render.slack_message("regression", hostile, "https://app.releaselens.test/")
    blob = json.dumps(msg)
    assert "<!channel>" not in blob
    assert "&lt;!channel&gt;" in blob
    assert "<https://evil.example|click>" not in blob
    link = f"https://app.releaselens.test/app/deployments/{PAYLOAD['deployment_id']}"
    assert f"<{link}|Open in ReleaseLens>" in blob
    assert msg["unfurl_links"] is False


def test_email_subject_is_one_line_and_risk_messages_render() -> None:
    subject, body = render.email_message(
        "regression", {**PAYLOAD, "service": "pay\r\nBcc: x@example.com"}, "https://app.test"
    )
    assert "\n" not in subject
    assert "\r" not in subject
    assert "https://app.test/app/deployments/" in body
    risk = {"risk_assessment_id": "ra-1", "band": "CRITICAL", "risk_score": 91, "service": "api"}
    assert render.headline("risk", risk) == "Pre-deploy risk CRITICAL (risk score 91/100) for api"
    assert render.link_for("risk", risk, "https://app.test") == "https://app.test/app/risk/ra-1"


def _sender(handler: object) -> SlackSender:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)  # type: ignore[arg-type]
    return SlackSender(client)


async def test_slack_success_posts_json() -> None:
    seen: list[httpx.Request] = []

    def ok(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ok")

    await _sender(ok).send("https://hooks.slack.com/services/T/B/x", {"text": "hi"})
    assert json.loads(seen[0].content) == {"text": "hi"}
    assert seen[0].headers["content-type"] == "application/json"


@pytest.mark.parametrize(
    ("response", "permanent", "retry_after"),
    [
        (httpx.Response(429, headers={"Retry-After": "7"}, text="rate_limited"), False, 7.0),
        (httpx.Response(503, text="service unavailable"), False, None),
        (httpx.Response(404, text="no_service"), True, None),
        (httpx.Response(403, text="invalid_token"), True, None),
        (httpx.Response(302, headers={"Location": "http://169.254.169.254/"}), True, None),
    ],
)
async def test_slack_failures_are_classified(
    response: httpx.Response, permanent: bool, retry_after: float | None
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return response

    with pytest.raises(DeliveryError) as info:
        await _sender(handler).send("https://hooks.slack.com/services/T/B/x", {"text": "hi"})
    assert info.value.permanent is permanent
    assert info.value.retry_after_s == retry_after
    assert calls == ["https://hooks.slack.com/services/T/B/x"]  # a redirect is never followed


async def test_slack_timeouts_and_connection_errors_are_transient() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    for handler in (timeout, refused):
        with pytest.raises(DeliveryError) as info:
            await _sender(handler).send("https://hooks.slack.com/services/T/B/x", {})
        assert info.value.permanent is False

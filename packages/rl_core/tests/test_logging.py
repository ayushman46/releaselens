import json
import logging
import uuid

import pytest

from rl_core.obs import JsonFormatter, bind_log_context, reset_log_context
from rl_core.tenancy import Principal, TenantContext


def _format(record: logging.LogRecord) -> dict[str, object]:
    return json.loads(JsonFormatter("test-svc").format(record))


def _record(msg: str = "hello", **extra: object) -> logging.LogRecord:
    rec = logging.LogRecord("rl.test", logging.INFO, __file__, 1, msg, None, None)
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def test_json_formatter_core_fields() -> None:
    out = _format(_record())
    assert out["msg"] == "hello"
    assert out["level"] == "info"
    assert out["service"] == "test-svc"
    assert out["logger"] == "rl.test"
    assert str(out["ts"]).endswith("+00:00")


def test_json_formatter_includes_bound_context_and_extra() -> None:
    token = bind_log_context(request_id="req-1", tenant_id="t-1")
    try:
        out = _format(_record(path="/x", status=200))
    finally:
        reset_log_context(token)
    assert out["request_id"] == "req-1"
    assert out["tenant_id"] == "t-1"
    assert out["path"] == "/x"
    assert out["status"] == 200


def test_json_formatter_includes_otel_trace_ids() -> None:
    from opentelemetry.sdk.trace import TracerProvider

    tracer = TracerProvider().get_tracer("t")
    with tracer.start_as_current_span("s") as span:
        out = _format(_record())
        ctx = span.get_span_context()
    assert out["trace_id"] == f"{ctx.trace_id:032x}"
    assert out["span_id"] == f"{ctx.span_id:016x}"


def test_json_formatter_serialises_exceptions() -> None:
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        import sys

        rec = logging.LogRecord(
            "rl.test", logging.ERROR, __file__, 1, "failed", None, sys.exc_info()
        )
    out = _format(rec)
    assert out["exc_type"] == "RuntimeError"
    assert "boom" in str(out["exc"])


def test_tenant_context_requires_uuid() -> None:
    with pytest.raises(TypeError):
        TenantContext(tenant_id="not-a-uuid", principal=Principal.API_KEY)  # type: ignore[arg-type]
    ctx = TenantContext(
        tenant_id=uuid.uuid4(), principal=Principal.API_KEY, scopes=frozenset({"read"})
    )
    assert ctx.has_scope("read")
    assert not ctx.has_scope("deployments:write")

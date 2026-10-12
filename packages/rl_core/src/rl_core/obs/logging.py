"""JSON line logging with OpenTelemetry trace correlation.

One JSON object per line: ``ts``, ``level``, ``logger``, ``msg``, ``service`` and, when available,
``trace_id``/``span_id`` (from the active OTel span), any fields bound via
:func:`rl_core.obs.bind_log_context`, and ``extra=`` fields passed to the logger.
OpenTelemetry is optional: without ``opentelemetry-api`` installed, trace fields are omitted.
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import UTC, datetime
from typing import Any

from rl_core.obs.context import get_log_context

try:  # pragma: no cover - exercised implicitly when OTel is installed
    from opentelemetry import trace as _otel_trace
except ImportError:  # pragma: no cover
    _otel_trace = None  # type: ignore[assignment]

# Attributes present on every LogRecord; anything else came from ``extra=``.
_RESERVED = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()
    | {"message", "asctime", "taskName", "color_message"}
)


def _trace_fields() -> dict[str, str]:
    if _otel_trace is None:
        return {}
    ctx = _otel_trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return {}
    return {"trace_id": f"{ctx.trace_id:032x}", "span_id": f"{ctx.span_id:016x}"}


def _jsonable(value: Any) -> Any:
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_jsonable(v) for v in value]
    return str(value)


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
            "service": self.service,
        }
        payload.update(_trace_fields())
        payload.update({k: _jsonable(v) for k, v in get_log_context().items()})
        for key, value in vars(record).items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = _jsonable(value)
        if record.exc_info:
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            payload["exc"] = "".join(traceback.format_exception(*record.exc_info)).rstrip()
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class ConsoleFormatter(logging.Formatter):
    """Human-friendly formatter for local debugging (``RL_LOG_FORMAT=console``)."""

    def format(self, record: logging.LogRecord) -> str:
        ctx = {**_trace_fields(), **get_log_context()}
        suffix = " ".join(f"{k}={v}" for k, v in ctx.items())
        base = f"{record.levelname:<7} {record.name}: {record.getMessage()}"
        if record.exc_info:
            base += "\n" + "".join(traceback.format_exception(*record.exc_info)).rstrip()
        return f"{base} {suffix}".rstrip()


class _StdoutHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Writes to whatever ``sys.stdout`` is *now* (test capture, redirection), not at init time."""

    def __init__(self) -> None:
        super().__init__(sys.stdout)

    @property
    def stream(self) -> Any:
        return sys.stdout

    @stream.setter
    def stream(self, _value: Any) -> None:
        pass


def configure_logging(service: str, level: str = "INFO", fmt: str = "json") -> None:
    """Configure the root logger once per process. Idempotent."""
    handler = _StdoutHandler()
    handler.setFormatter(JsonFormatter(service) if fmt == "json" else ConsoleFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Route uvicorn's loggers through the root handler instead of their own.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
    logging.getLogger("uvicorn.access").disabled = True  # replaced by our access-log middleware

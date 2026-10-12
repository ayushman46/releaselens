"""Per-task logging context (request id, tenant id, …) carried in a ContextVar.

Works across ``await`` boundaries and is isolated between concurrent asyncio tasks.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from types import MappingProxyType
from typing import Any

_EMPTY: MappingProxyType[str, Any] = MappingProxyType({})
_log_context: ContextVar[MappingProxyType[str, Any]] = ContextVar("rl_log_context", default=_EMPTY)


def bind_log_context(**fields: Any) -> Token[MappingProxyType[str, Any]]:
    """Add fields to the current logging context. Returns a token for :func:`reset_log_context`."""
    merged = {**_log_context.get(), **{k: v for k, v in fields.items() if v is not None}}
    return _log_context.set(MappingProxyType(merged))


def reset_log_context(token: Token[MappingProxyType[str, Any]]) -> None:
    _log_context.reset(token)


def get_log_context() -> MappingProxyType[str, Any]:
    return _log_context.get()

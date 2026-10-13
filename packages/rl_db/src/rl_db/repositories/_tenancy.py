"""Shared guards for tenant-scoped repository functions.

Two independent guards (ADR-0010):

1. every query names ``tenant_id`` explicitly, and writes check the row's tenant against the
   caller's context (:func:`require_tenant`);
2. :func:`tenant_scoped` binds the transaction to the caller's tenant before the function runs
   (:func:`bind_tenant`): ``SET LOCAL ROLE rl_tenant`` plus ``SET LOCAL app.tenant_id``, so
   Postgres row-level security (migration ``0007_row_level_security``) filters every statement
   even if guard 1 was forgotten.

Both settings are transaction-local, so they end with the transaction. Cross-tenant code lives
in :mod:`rl_db.admin` and is decorated with :func:`cross_tenant`, which switches a bound
transaction back to the connecting role first, so admin and tenant calls can share a
transaction in either order.
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, Concatenate, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from rl_core.tenancy import TenantContext

TENANT_ROLE: Final = "rl_tenant"
_BIND = text("SELECT set_config('role', :role, true), set_config('app.tenant_id', :tenant, true)")
_UNBIND = text("SELECT set_config('role', 'none', true), set_config('app.tenant_id', '', true)")
# Set while this session/connection may be bound, so unbound admin calls cost no round trip.
_BOUND_FLAG: Final = "rl_tenant_bound"


class TenantMismatchError(ValueError):
    """A write tried to stamp a row with a tenant other than the caller's context."""


def require_tenant(ctx: TenantContext, tenant_id: uuid.UUID) -> None:
    if not isinstance(ctx, TenantContext):  # defends against duck-typed or None contexts
        raise TypeError("a TenantContext is required")
    if ctx.tenant_id != tenant_id:
        raise TenantMismatchError("row tenant does not match the caller's tenant context")


async def bind_tenant(session: AsyncSession, ctx: TenantContext) -> None:
    """Make the rest of this transaction run as ``rl_tenant`` for ``ctx``'s tenant only."""
    if not isinstance(ctx, TenantContext):
        raise TypeError("a TenantContext is required")
    session.info[_BOUND_FLAG] = True
    await session.execute(_BIND, {"role": TENANT_ROLE, "tenant": str(ctx.tenant_id)})


async def unbind_tenant(executor: AsyncSession | AsyncConnection) -> None:
    """Back to the connecting role for the rest of this transaction (cross-tenant code)."""
    if executor.info.pop(_BOUND_FLAG, False):
        await executor.execute(_UNBIND)


def tenant_scoped[**P, R](
    fn: Callable[Concatenate[AsyncSession, TenantContext, P], Awaitable[R]],
) -> Callable[Concatenate[AsyncSession, TenantContext, P], Coroutine[Any, Any, R]]:
    """Decorate a ``(session, ctx, ...)`` repository function: bind first, then run."""

    @functools.wraps(fn)
    async def wrapper(
        session: AsyncSession, ctx: TenantContext, /, *args: P.args, **kwargs: P.kwargs
    ) -> R:
        await bind_tenant(session, ctx)
        return await fn(session, ctx, *args, **kwargs)

    return wrapper


def cross_tenant[S: AsyncSession | AsyncConnection, **P, R](
    fn: Callable[Concatenate[S, P], Awaitable[R]],
) -> Callable[Concatenate[S, P], Coroutine[Any, Any, R]]:
    """Decorate an :mod:`rl_db.admin` function whose first argument is the session."""

    @functools.wraps(fn)
    async def wrapper(executor: S, /, *args: P.args, **kwargs: P.kwargs) -> R:
        await unbind_tenant(executor)
        return await fn(executor, *args, **kwargs)

    return wrapper

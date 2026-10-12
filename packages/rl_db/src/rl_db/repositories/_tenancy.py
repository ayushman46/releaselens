"""Shared guard for tenant-scoped repository functions."""

from __future__ import annotations

import uuid

from rl_core.tenancy import TenantContext


class TenantMismatchError(ValueError):
    """A write tried to stamp a row with a tenant other than the caller's context."""


def require_tenant(ctx: TenantContext, tenant_id: uuid.UUID) -> None:
    if not isinstance(ctx, TenantContext):  # defends against duck-typed or None contexts
        raise TypeError("a TenantContext is required")
    if ctx.tenant_id != tenant_id:
        raise TenantMismatchError("row tenant does not match the caller's tenant context")

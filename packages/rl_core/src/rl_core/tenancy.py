"""Tenant context.

Every repository function that touches tenant-owned data takes a :class:`TenantContext`.
It is created only by the authentication layer (API key or session) and never from
client-supplied headers or body fields.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum


class Principal(StrEnum):
    """How the caller was authenticated."""

    API_KEY = "api_key"
    USER_SESSION = "user_session"
    SYSTEM = "system"  # internal workers acting on behalf of a tenant (e.g. Kafka consumers)


@dataclass(frozen=True, slots=True)
class TenantContext:
    tenant_id: uuid.UUID
    principal: Principal
    actor_id: uuid.UUID | None = None  # user id or api key id
    scopes: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, uuid.UUID):
            raise TypeError("tenant_id must be a uuid.UUID")

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def log_fields(self) -> dict[str, str]:
        return {"tenant_id": str(self.tenant_id), "principal": self.principal.value}

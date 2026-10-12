"""Async engine and session factory."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(
    url: str,
    *,
    echo: bool = False,
    pool_size: int = 5,
    connect_timeout_s: float | None = None,
    command_timeout_s: float | None = None,
) -> AsyncEngine:
    """Create the asyncpg engine.

    The timeouts are opt-in and ``None`` keeps asyncpg's defaults. Long-running consumers
    set both, so a database that stops answering (network partition, paused VM) raises a
    timeout they can classify as transient and retry, instead of hanging the loop forever.
    """
    if not url.startswith("postgresql+asyncpg://"):
        raise ValueError("RL_DATABASE_URL must use the postgresql+asyncpg:// driver")
    connect_args: dict[str, Any] = {"server_settings": {"application_name": "releaselens"}}
    if connect_timeout_s is not None:
        connect_args["timeout"] = connect_timeout_s
    if command_timeout_s is not None:
        connect_args["command_timeout"] = command_timeout_s
    return create_async_engine(
        url,
        echo=echo,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=pool_size,
        connect_args=connect_args,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

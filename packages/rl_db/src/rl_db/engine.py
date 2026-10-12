"""Async engine and session factory."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(url: str, *, echo: bool = False, pool_size: int = 5) -> AsyncEngine:
    if not url.startswith("postgresql+asyncpg://"):
        raise ValueError("RL_DATABASE_URL must use the postgresql+asyncpg:// driver")
    return create_async_engine(
        url,
        echo=echo,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=pool_size,
        connect_args={"server_settings": {"application_name": "releaselens"}},
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

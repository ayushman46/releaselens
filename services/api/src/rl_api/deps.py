"""Shared request dependencies: database session and the event producer."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from rl_api.kafka import EventProducer


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request. Routes that write open ``session.begin()`` themselves."""
    async with request.app.state.db_sessions() as session:
        yield session


def get_producer(request: Request) -> EventProducer:
    producer: EventProducer = request.app.state.event_producer
    return producer

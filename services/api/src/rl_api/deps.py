"""Shared request dependencies: database session, event producer and the M10 services
(settings, mailer, password hasher, outgoing HTTP client, billing gateway)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import httpx
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rl_api.kafka import EventProducer

if TYPE_CHECKING:
    from rl_api.billing import BillingGateway
    from rl_api.mail import Mailer
    from rl_api.passwords import PasswordHasher
    from rl_api.settings import Settings
    from rl_db.secretbox import SecretBox


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request. Routes that write open ``session.begin()`` themselves."""
    async with request.app.state.db_sessions() as session:
        yield session


def get_producer(request: Request) -> EventProducer:
    producer: EventProducer = request.app.state.event_producer
    return producer


def get_sessions(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.db_sessions
    return factory


def get_app_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_mailer(request: Request) -> Mailer:
    mailer: Mailer = request.app.state.mailer
    return mailer


def get_hasher(request: Request) -> PasswordHasher:
    hasher: PasswordHasher = request.app.state.password_hasher
    return hasher


def get_http_client(request: Request) -> httpx.AsyncClient:
    client: httpx.AsyncClient = request.app.state.http_client
    return client


def get_secret_box(request: Request) -> SecretBox:
    box: SecretBox = request.app.state.secret_box
    return box


def get_billing_gateway(request: Request) -> BillingGateway | None:
    gateway: BillingGateway | None = request.app.state.billing_gateway
    return gateway

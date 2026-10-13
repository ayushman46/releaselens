"""Login sessions, one-time user tokens and OAuth state (M10; docs/security/AUTH.md).

Every secret here is stored as ``sha256(token)`` (hex). The tokens are 256-bit random values,
so a fast hash is enough (same reasoning as API keys, ADR-0016), and a database leak does
not reveal a usable token. Users are global, so none of these tables carry ``tenant_id``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from ipaddress import IPv4Address, IPv6Address

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import INET, UUID
from sqlalchemy.orm import Mapped, mapped_column

from rl_db.base import Base, UUIDPrimaryKeyMixin

TOKEN_HASH_LEN = 64  # hex SHA-256


class AuthSession(UUIDPrimaryKeyMixin, Base):
    """One login. Its id is the *family* id of the refresh-token chain: presenting an already
    rotated refresh token revokes the whole row (reuse detection)."""

    __tablename__ = "auth_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # The one valid access token of this session; rotated on every refresh.
    access_token_hash: Mapped[str] = mapped_column(
        String(TOKEN_HASH_LEN), nullable=False, unique=True
    )
    access_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Absolute end of the session: refreshes never extend past it.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    auth_method: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(32))
    # asyncpg returns ipaddress objects for INET; str() gives the canonical text form.
    ip: Mapped[IPv4Address | IPv6Address | str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(512))

    __table_args__ = (
        Index("ix_auth_sessions_user_id", "user_id"),
        CheckConstraint("auth_method IN ('password', 'github')", name="auth_method_known"),
    )


class RefreshToken(UUIDPrimaryKeyMixin, Base):
    """One link of a session's refresh-token chain. ``used_at`` is set when it is rotated;
    presenting it again after that is reuse."""

    __tablename__ = "auth_refresh_tokens"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("auth_sessions.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(TOKEN_HASH_LEN), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_auth_refresh_tokens_session_id", "session_id"),)


class TokenPurpose:
    VERIFY_EMAIL = "verify_email"
    RESET_PASSWORD = "reset_password"  # noqa: S105 - a purpose label, not a password


class UserToken(UUIDPrimaryKeyMixin, Base):
    """Single-use e-mailed tokens: email verification (24 h) and password reset (1 h)."""

    __tablename__ = "user_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(TOKEN_HASH_LEN), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_user_tokens_user_id_purpose", "user_id", "purpose"),
        CheckConstraint("purpose IN ('verify_email', 'reset_password')", name="purpose_known"),
    )


class OAuthState(Base):
    """Pending GitHub sign-in: the PKCE verifier for one ``state`` value (10 minutes, single
    use: the callback deletes the row)."""

    __tablename__ = "oauth_states"

    state_hash: Mapped[str] = mapped_column(String(TOKEN_HASH_LEN), primary_key=True)
    code_verifier: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

"""Map arbitrary exceptions onto the permanent/transient taxonomy (rl_core.events.errors).

The consumer's behaviour depends only on the class this returns:

* transient: connection loss, timeouts, resource exhaustion, serialization failures and
  deadlocks. The same message will very likely succeed later, so retry and never skip it.
* permanent: everything else, including bugs. It goes to the DLQ, which keeps the full
  message and stack for replay after a fix.

Defaulting unknown errors to *permanent* is a deliberate trade-off. The alternative,
retrying forever, would stop the whole partition on a single poison message. The DLQ has
an ``error_type`` header (``unhandled_exception`` for this case), so the replay tooling can
pick these messages out.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import exc as sa_exc

from rl_core.events import EventError, PermanentError, TransientError, UnknownTenantError

# SQLSTATE classes/codes that describe the server or connection, not the data.
_TRANSIENT_SQLSTATE_PREFIXES = (
    "08",  # connection exception
    "53",  # insufficient resources (too many connections, disk full, out of memory)
    "57P",  # operator intervention: admin/crash shutdown, cannot connect now
    "58",  # system error (I/O)
)
_TRANSIENT_SQLSTATES = frozenset(
    {
        "40001",  # serialization_failure
        "40P01",  # deadlock_detected
        "55P03",  # lock_not_available
        "57014",  # query_canceled (statement_timeout)
    }
)
_FOREIGN_KEY_VIOLATION = "23503"


class UnhandledProcessingError(PermanentError):
    """An exception nobody classified. Most likely a bug; replay from the DLQ after a fix."""

    error_type = "unhandled_exception"


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """``exc``, then what it wraps: SQLAlchemy's ``.orig`` (the DBAPI error carrying the
    SQLSTATE), then ``__cause__``/``__context__``."""
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        orig = getattr(current, "orig", None)
        if isinstance(orig, BaseException):
            pending.append(orig)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        elif current.__context__ is not None:
            pending.append(current.__context__)


def _is_asyncpg_connection_error(exc: BaseException) -> bool:
    try:
        import asyncpg
    except ImportError:  # pragma: no cover - asyncpg is a hard dependency of rl_db
        return False
    return isinstance(
        exc,
        asyncpg.exceptions.InterfaceError  # includes ConnectionDoesNotExistError
        | asyncpg.exceptions.PostgresConnectionError
        | asyncpg.exceptions.CannotConnectNowError
        | asyncpg.exceptions.TooManyConnectionsError,
    )


def classify(exc: BaseException) -> EventError:
    """Return the taxonomy error for ``exc`` (``exc`` itself if it is already classified)."""
    if isinstance(exc, EventError):
        return exc
    for link in _chain(exc):
        sqlstate = getattr(link, "sqlstate", None) or getattr(link, "pgcode", None)
        if isinstance(sqlstate, str):
            if sqlstate == _FOREIGN_KEY_VIOLATION:
                # The only FK a deployment insert can violate is tenant_id -> organizations.
                return UnknownTenantError("the event's tenant does not exist")
            if sqlstate in _TRANSIENT_SQLSTATES or sqlstate.startswith(
                _TRANSIENT_SQLSTATE_PREFIXES
            ):
                return TransientError(f"database unavailable (SQLSTATE {sqlstate})")
        if isinstance(link, sa_exc.DBAPIError) and link.connection_invalidated:
            return TransientError("database connection lost")
        if isinstance(
            link,
            sa_exc.OperationalError
            | sa_exc.InterfaceError
            | sa_exc.DisconnectionError
            | sa_exc.TimeoutError,  # connection-pool checkout timeout
        ):
            return TransientError(f"database unavailable ({type(link).__name__})")
        if isinstance(link, TimeoutError | ConnectionError | OSError):
            return TransientError(f"dependency unavailable ({type(link).__name__})")
        if _is_asyncpg_connection_error(link):
            return TransientError(f"database connection lost ({type(link).__name__})")
    return UnhandledProcessingError(f"{type(exc).__name__}: {exc}")

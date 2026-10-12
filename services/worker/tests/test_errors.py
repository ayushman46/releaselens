"""Exception classification: permanent vs transient (drives DLQ vs retry)."""

from __future__ import annotations

import asyncpg
import pytest
from sqlalchemy import exc as sa_exc

from rl_core.events import (
    EventValidationError,
    MalformedEventError,
    PermanentError,
    TransientError,
    UnknownTenantError,
)
from rl_worker.errors import UnhandledProcessingError, classify


class _PgError(Exception):
    """Stands in for asyncpg/SQLAlchemy-adapted errors, which expose ``sqlstate``."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"sqlstate {sqlstate}")
        self.sqlstate = sqlstate


def _wrapped(orig: BaseException) -> sa_exc.DBAPIError:
    return sa_exc.DBAPIError("INSERT ...", {}, orig)  # type: ignore[arg-type]


def test_classified_errors_pass_through() -> None:
    err = MalformedEventError("bad")
    assert classify(err) is err
    assert isinstance(classify(EventValidationError("x")), PermanentError)


@pytest.mark.parametrize(
    "sqlstate", ["08006", "08001", "53300", "57P01", "57P03", "58030", "40001", "40P01", "57014"]
)
def test_server_and_connection_sqlstates_are_transient(sqlstate: str) -> None:
    assert isinstance(classify(_wrapped(_PgError(sqlstate))), TransientError)


def test_foreign_key_violation_means_unknown_tenant() -> None:
    error = classify(sa_exc.IntegrityError("INSERT", {}, _PgError("23503")))
    assert isinstance(error, UnknownTenantError)
    assert error.error_type == "unknown_tenant"


@pytest.mark.parametrize("sqlstate", ["23514", "22001", "42P01"])
def test_data_and_schema_errors_are_permanent(sqlstate: str) -> None:
    assert isinstance(classify(_wrapped(_PgError(sqlstate))), PermanentError)


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionRefusedError(61, "refused"),
        TimeoutError(),
        OSError("network unreachable"),
        sa_exc.OperationalError("SELECT 1", {}, Exception("server closed the connection")),
        sa_exc.InterfaceError("SELECT 1", {}, Exception("connection is closed")),
        sa_exc.TimeoutError("QueuePool limit reached"),
        asyncpg.exceptions.ConnectionDoesNotExistError("closed"),
        asyncpg.exceptions.CannotConnectNowError("starting up"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_connection_level_failures_are_transient(exc: BaseException) -> None:
    assert isinstance(classify(exc), TransientError)


def test_cause_chain_is_followed() -> None:
    outer = RuntimeError("while inserting")
    outer.__cause__ = ConnectionResetError("peer reset")
    assert isinstance(classify(outer), TransientError)


def test_invalidated_connection_is_transient() -> None:
    err = sa_exc.DBAPIError("SELECT 1", {}, Exception("x"), connection_invalidated=True)
    assert isinstance(classify(err), TransientError)


def test_unknown_errors_are_permanent_and_labelled() -> None:
    error = classify(KeyError("bug"))
    assert isinstance(error, UnhandledProcessingError)
    assert error.error_type == "unhandled_exception"
    assert "KeyError" in str(error)

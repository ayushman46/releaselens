"""Error taxonomy for event processing (spec §10).

Consumers decide what to do with a failed message from the *class* of the error alone:

* :class:`PermanentError`: retrying the same bytes can never succeed (bad JSON, unknown
  schema version, validation failure, unknown tenant). The message goes to the DLQ with
  metadata, and its offset is committed so the partition keeps moving.
* :class:`TransientError`: the message is fine but a dependency is not (database
  connection lost, timeout). The consumer retries with backoff and never skips the message.

Each permanent subclass has a stable ``error_type`` string. It is written to the DLQ
``error_type`` header, so operators can filter and replay by category.
"""

from __future__ import annotations

from typing import ClassVar


class EventError(Exception):
    """Base class for every classified event-processing error."""

    error_type: ClassVar[str] = "event_error"


class PermanentError(EventError):
    """Retrying cannot help; dead-letter the message and move on."""

    error_type = "permanent"


class EventTooLargeError(PermanentError):
    error_type = "event_too_large"


class MalformedEventError(PermanentError):
    """Not UTF-8 JSON, or not a JSON object."""

    error_type = "malformed_json"


class UnknownSchemaVersionError(PermanentError):
    error_type = "unknown_schema_version"


class UnknownEventTypeError(PermanentError):
    error_type = "unknown_event_type"


class EventValidationError(PermanentError):
    """The envelope parsed, but a field is missing, has the wrong type or breaks a limit.

    ``errors`` carries pydantic's location, type and message for each problem. It never
    carries the rejected *input values*: those can be large, and they can contain customer
    data that must not be copied into logs or headers.
    """

    error_type = "validation_error"

    def __init__(self, message: str, errors: list[dict[str, str]] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


class UnknownTenantError(PermanentError):
    """The envelope names a tenant that does not exist (or no longer exists)."""

    error_type = "unknown_tenant"


class TransientError(EventError):
    """A dependency failed; the same message may succeed later. Retry, never skip."""

    error_type = "transient"

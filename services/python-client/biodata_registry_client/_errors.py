"""
Typed exception classes for the Python client.

These mirror the server-side classes in
``services/shared-layer/biodata_registry_shared/errors.py`` (R30.5,
Property 14). Two copies, by design:

* The shared Layer copy is what the Lambdas *raise* and renders the
  Property 14 wire envelope on the way out.
* This client copy is what callers *catch*. Decoupling avoids
  forcing every external Python consumer to install the Lambda Layer's
  fat dependency set (``aind-data-schema``, ``psycopg``,
  ``openapi-core``) just to get exception classes.

The wire ``code`` strings are the contract — both copies must agree
on them. :class:`ErrorCode`'s values are kept byte-identical with the
Lambda Layer; if you add a new code, add it in *both* files (and the
OpenAPI ``ErrorResponse.json`` schema). A small contract test in
``tests/test_errors_match_layer.py`` enforces the shared subset where
the Layer is importable.

The exception hierarchy intentionally drops the response-shaping
helpers (``make_error_response``, ``error_response_from_exception``)
from the Layer copy — clients never *emit* errors over HTTP, so those
helpers would be dead weight here.
"""

from __future__ import annotations

import enum
from typing import Any, Mapping, Optional


# ---------------------------------------------------------------------------
# Error code enumeration (must match shared-layer/biodata_registry_shared/errors.py).
# ---------------------------------------------------------------------------


class ErrorCode(str, enum.Enum):
    """Closed set of error codes the registry can emit (Property 14)."""

    # Closed business set (design.md §Error Code Mapping).
    VALIDATION_FAILED = "VALIDATION_FAILED"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    INVALID_HIERARCHY = "INVALID_HIERARCHY"
    MISSING_PROVENANCE = "MISSING_PROVENANCE"
    DUPLICATE_ENTITY = "DUPLICATE_ENTITY"
    FORBIDDEN = "FORBIDDEN"
    SENSITIVE_ACCESS_DENIED = "SENSITIVE_ACCESS_DENIED"
    RATE_LIMITED = "RATE_LIMITED"

    # Operational codes (HTTP-shaped, not in the closed business set).
    UNAUTHORIZED = "UNAUTHORIZED"
    NOT_FOUND = "NOT_FOUND"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# Typed exceptions.
# ---------------------------------------------------------------------------


class RegistryError(Exception):
    """Base class for every error the registry surfaces over HTTP.

    All Property 14 fields land on the instance:

    * :attr:`code` — server-emitted ``ErrorCode`` value.
    * :attr:`message` — human-readable summary.
    * :attr:`details` — JSON-serializable per-field structure (or ``None``).
    * :attr:`request_id` — API Gateway request id for log correlation.
    * :attr:`http_status` — the HTTP status code that produced this
      exception. Useful for code that wants to inspect the wire
      response shape without re-deriving it from the code.
    * :attr:`retry_after_s` — populated only for :class:`RateLimited`,
      hoisted to the base class so generic ``except RegistryError``
      handlers can read it without ``isinstance`` gymnastics.
    """

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    default_message: str = "Internal server error"

    def __init__(
        self,
        message: Optional[str] = None,
        *,
        details: Any = None,
        request_id: Optional[str] = None,
        http_status: Optional[int] = None,
        retry_after_s: Optional[int] = None,
    ) -> None:
        super().__init__(message or self.default_message)
        self.message: str = message or self.default_message
        self.details: Any = details
        self.request_id: Optional[str] = request_id
        self.http_status: Optional[int] = http_status
        self.retry_after_s: Optional[int] = retry_after_s

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self.code.value!r}, "
            f"message={self.message!r}, http_status={self.http_status!r}, "
            f"request_id={self.request_id!r})"
        )


class ValidationFailed(RegistryError):
    """422 — aind-data-schema (or Custom_Schema) validation rejected the payload."""

    code = ErrorCode.VALIDATION_FAILED
    default_message = "Metadata failed schema validation"


class InvalidStateTransition(RegistryError):
    """409 — Lifecycle_Lambda rejected a state transition.

    ``details`` typically includes ``{current_state, allowed_transitions}``.
    """

    code = ErrorCode.INVALID_STATE_TRANSITION
    default_message = "State transition not allowed"


class InvalidHierarchy(RegistryError):
    """400 — Collections_Lambda detected a cycle in ``collection_hierarchy``."""

    code = ErrorCode.INVALID_HIERARCHY
    default_message = "Collection hierarchy would create a cycle"


class MissingProvenance(RegistryError):
    """400 — Derived asset created without ``provenance_source_id``."""

    code = ErrorCode.MISSING_PROVENANCE
    default_message = "Derived assets require a provenance link"


class DuplicateEntity(RegistryError):
    """409 — Unique-constraint violation on ``data_asset.storage_uri``."""

    code = ErrorCode.DUPLICATE_ENTITY
    default_message = "An entity with this storage_uri already exists"


class Forbidden(RegistryError):
    """403 — Role check failed."""

    code = ErrorCode.FORBIDDEN
    default_message = "Caller is not authorized for this action"


class SensitiveAccessDenied(RegistryError):
    """403 — Sensitive_Flag resource accessed by non-privileged caller (R8)."""

    code = ErrorCode.SENSITIVE_ACCESS_DENIED
    default_message = "Caller is not authorized to access sensitive data"


class RateLimited(RegistryError):
    """429 — API Gateway usage plan exceeded.

    :attr:`retry_after_s` is populated from the ``Retry-After`` header
    when present, falling back to ``details["retry_after_s"]`` when
    the server included it in the body.
    """

    code = ErrorCode.RATE_LIMITED
    default_message = "Rate limit exceeded"


class Unauthorized(RegistryError):
    """401 — JWT missing / expired / invalid."""

    code = ErrorCode.UNAUTHORIZED
    default_message = "Authentication required"


class NotFound(RegistryError):
    """404 — Resource does not exist (or is hidden by RLS).

    Note the RLS-hides-as-404 design: a 404 from a GET does not
    necessarily mean the resource is absent. It may exist but be
    invisible to the caller. This avoids existence side-channels.
    """

    code = ErrorCode.NOT_FOUND
    default_message = "Resource not found"


class Conflict(RegistryError):
    """409 — Generic conflict (mirrors the shared-layer alias).

    The shared-layer copy aliases this to ``INVALID_STATE_TRANSITION``
    for wire compatibility; we do the same. Callers can catch
    ``Conflict`` to handle "this resource is in a state that prevents
    the operation" cases generically.
    """

    code = ErrorCode.INVALID_STATE_TRANSITION
    default_message = "Conflict with current resource state"


# ---------------------------------------------------------------------------
# Code → exception lookup.
# ---------------------------------------------------------------------------


_EXCEPTION_BY_CODE: Mapping[ErrorCode, type[RegistryError]] = {
    ErrorCode.VALIDATION_FAILED: ValidationFailed,
    ErrorCode.INVALID_STATE_TRANSITION: InvalidStateTransition,
    ErrorCode.INVALID_HIERARCHY: InvalidHierarchy,
    ErrorCode.MISSING_PROVENANCE: MissingProvenance,
    ErrorCode.DUPLICATE_ENTITY: DuplicateEntity,
    ErrorCode.FORBIDDEN: Forbidden,
    ErrorCode.SENSITIVE_ACCESS_DENIED: SensitiveAccessDenied,
    ErrorCode.RATE_LIMITED: RateLimited,
    ErrorCode.UNAUTHORIZED: Unauthorized,
    ErrorCode.NOT_FOUND: NotFound,
    ErrorCode.INTERNAL_ERROR: RegistryError,
}


def exception_for_code(code: ErrorCode | str) -> type[RegistryError]:
    """Map a wire code value to the corresponding typed exception class.

    Unknown codes (not in :class:`ErrorCode`) collapse to the
    :class:`RegistryError` base, so a forward-compatible client never
    crashes on a code it doesn't yet recognize — the caller can still
    inspect ``exc.code`` and ``exc.http_status`` and decide what to do.
    """
    if not isinstance(code, ErrorCode):
        try:
            code = ErrorCode(code)
        except ValueError:
            return RegistryError
    return _EXCEPTION_BY_CODE.get(code, RegistryError)


__all__ = (
    "Conflict",
    "DuplicateEntity",
    "ErrorCode",
    "Forbidden",
    "InvalidHierarchy",
    "InvalidStateTransition",
    "MissingProvenance",
    "NotFound",
    "RateLimited",
    "RegistryError",
    "SensitiveAccessDenied",
    "Unauthorized",
    "ValidationFailed",
    "exception_for_code",
)

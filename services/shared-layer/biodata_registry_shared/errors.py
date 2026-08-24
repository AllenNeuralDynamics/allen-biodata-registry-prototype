"""
Allen BioData Registry PoC — standardized error responses.

Every API Gateway-fronted Lambda in the registry emits errors in the
shape required by **Property 14** (R30):

.. code-block:: json

    {
      "code": "VALIDATION_FAILED",
      "message": "Metadata failed schema validation",
      "details": [...],
      "request_id": "01J9...",
      "timestamp": "2026-03-24T19:22:01.245Z"
    }

This module gives Lambdas:

* The :class:`ErrorCode` enum, kept in lockstep with the design's
  Error Code Mapping table.
* Typed exception classes (one per error code) so business code can
  raise an exception in idiomatic Python and let the framework shape
  it into the correct HTTP response.
* :func:`make_error_response` — programmatic shaper for code that
  prefers building the dict directly.
* :func:`error_response_from_exception` — adapter that turns any of
  the typed exceptions into the API Gateway proxy response shape
  ``{"statusCode": int, "headers": {...}, "body": json_str}``.
* :func:`exception_for_code` — reverse lookup used by the Python
  client (Task 13.2) to map server-returned codes to local typed
  exceptions per R30.5 / Property 14.

Validates: R30.1, R30.2, R30.3, R30.4, R30.5; design.md §Error Handling.
"""

from __future__ import annotations

import enum
import json
from datetime import datetime, timezone
from typing import Any, Mapping, Optional


# ---------------------------------------------------------------------------
# Error code enumeration
# ---------------------------------------------------------------------------


class ErrorCode(str, enum.Enum):
    """Closed set of error codes the registry can emit (Property 14).

    The values are stable wire identifiers — clients pattern-match on
    them, so adding a new code is a backward-compatible change but
    renaming an existing one is breaking.
    """

    VALIDATION_FAILED = "VALIDATION_FAILED"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    INVALID_HIERARCHY = "INVALID_HIERARCHY"
    MISSING_PROVENANCE = "MISSING_PROVENANCE"
    DUPLICATE_ENTITY = "DUPLICATE_ENTITY"
    FORBIDDEN = "FORBIDDEN"
    SENSITIVE_ACCESS_DENIED = "SENSITIVE_ACCESS_DENIED"
    RATE_LIMITED = "RATE_LIMITED"

    # Operational codes that are not part of the closed Property 14
    # set but are required for correct HTTP semantics. They are
    # intentionally NOT listed in design.md's Error Code Mapping table
    # because those codes are about *predictable* business outcomes;
    # 401/404/500 are about *operational* outcomes. The Python client
    # maps these too so callers get typed exceptions either way.
    UNAUTHORIZED = "UNAUTHORIZED"
    NOT_FOUND = "NOT_FOUND"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# Code → HTTP status mapping. Drawn directly from design.md "Error
# Code Mapping" table; the operational codes (UNAUTHORIZED / NOT_FOUND
# / INTERNAL_ERROR) follow the standard HTTP semantics.
_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_FAILED: 422,
    ErrorCode.INVALID_STATE_TRANSITION: 409,
    ErrorCode.INVALID_HIERARCHY: 400,
    ErrorCode.MISSING_PROVENANCE: 400,
    ErrorCode.DUPLICATE_ENTITY: 409,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.SENSITIVE_ACCESS_DENIED: 403,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.INTERNAL_ERROR: 500,
}


# ---------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------


class RegistryError(Exception):
    """Base class for every error the registry surfaces over HTTP.

    Holds the data needed to render a Property 14-shaped response. The
    ``code`` attribute is set by each subclass; callers should not
    instantiate :class:`RegistryError` directly.
    """

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    default_message: str = "Internal server error"

    def __init__(
        self,
        message: Optional[str] = None,
        *,
        details: Any = None,
        retry_after_s: Optional[int] = None,
    ) -> None:
        super().__init__(message or self.default_message)
        self.message: str = message or self.default_message
        self.details: Any = details
        # Retry-After header value (seconds). Only populated for
        # RateLimited; included on the base class so the response
        # adapter can read it without isinstance gymnastics.
        self.retry_after_s: Optional[int] = retry_after_s

    @property
    def http_status(self) -> int:
        return _HTTP_STATUS[self.code]


class ValidationFailed(RegistryError):
    """422 — aind-data-schema (or Custom_Schema) validation rejected the payload.

    ``details`` should be a list of ``{"field": str, "rule": str, ...}``
    entries per Property 14's per-field requirement.
    """

    code = ErrorCode.VALIDATION_FAILED
    default_message = "Metadata failed schema validation"


class InvalidStateTransition(RegistryError):
    """409 — Lifecycle_Lambda rejected a state transition.

    ``details`` should include ``{"current_state": str,
    "allowed_transitions": [str, ...]}`` per design Error Code Mapping.
    """

    code = ErrorCode.INVALID_STATE_TRANSITION
    default_message = "State transition not allowed"


class InvalidHierarchy(RegistryError):
    """400 — Collections_Lambda detected a cycle in ``collection_hierarchy``.

    ``details`` should include ``{"cycle_path": [collection_id, ...]}``.
    """

    code = ErrorCode.INVALID_HIERARCHY
    default_message = "Collection hierarchy would create a cycle"


class MissingProvenance(RegistryError):
    """400 — Derived asset created without a ``provenance_source_id``."""

    code = ErrorCode.MISSING_PROVENANCE
    default_message = "Derived assets require a provenance link"


class DuplicateEntity(RegistryError):
    """409 — Unique constraint violation on ``data_asset.storage_uri``.

    Note: similarity-based candidate matches do **not** raise this —
    they are surfaced as soft warnings on a 201 response (see Task 25.1).
    """

    code = ErrorCode.DUPLICATE_ENTITY
    default_message = "An entity with this storage_uri already exists"


class Forbidden(RegistryError):
    """403 — Role check failed (RLS Layer 1 or Layer 2).

    ``details`` should include ``{"required_role": str}`` whenever
    available.
    """

    code = ErrorCode.FORBIDDEN
    default_message = "Caller is not authorized for this action"


class SensitiveAccessDenied(RegistryError):
    """403 — Sensitive_Flag resource accessed by non-privileged caller (R8)."""

    code = ErrorCode.SENSITIVE_ACCESS_DENIED
    default_message = "Caller is not authorized to access sensitive data"


class RateLimited(RegistryError):
    """429 — API Gateway usage plan exceeded.

    Most commonly produced by API Gateway itself, but business Lambdas
    also raise this for self-imposed throttles (e.g. Bedrock token
    quotas). ``retry_after_s`` populates both the body's ``details``
    and the standard ``Retry-After`` HTTP header.
    """

    code = ErrorCode.RATE_LIMITED
    default_message = "Rate limit exceeded"

    def __init__(
        self,
        message: Optional[str] = None,
        *,
        retry_after_s: int,
        details: Any = None,
    ) -> None:
        # Auto-merge retry_after into details if the caller didn't
        # provide their own structure — matches the design table.
        if details is None:
            details = {"retry_after_s": retry_after_s}
        super().__init__(message, details=details, retry_after_s=retry_after_s)


# Operational exceptions: not Property 14 codes per se, but every
# Lambda needs the ability to raise them. Adding them here keeps the
# class hierarchy single-rooted at RegistryError so the response
# adapter can switch on the base class.


class Unauthorized(RegistryError):
    """401 — JWT missing / expired / invalid (raised pre-Authorizer)."""

    code = ErrorCode.UNAUTHORIZED
    default_message = "Authentication required"


class NotFound(RegistryError):
    """404 — Resource does not exist (or is invisible due to RLS).

    Note: when RLS hides a resource, returning 404 (not 403) is the
    canonical pattern — it prevents resource-existence side-channels.
    """

    code = ErrorCode.NOT_FOUND
    default_message = "Resource not found"


class Conflict(RegistryError):
    """409 — Generic conflict, e.g. an optimistic-locking version mismatch.

    Distinct from :class:`InvalidStateTransition` and
    :class:`DuplicateEntity` because not every 409 is one of those —
    e.g. a sharing-grant mutation that races another caller.
    """

    code = ErrorCode.INVALID_STATE_TRANSITION  # closest closed-set match
    default_message = "Conflict with current resource state"


# ---------------------------------------------------------------------------
# Response shapers
# ---------------------------------------------------------------------------


def make_error_response(
    code: ErrorCode | str,
    message: str,
    *,
    details: Any = None,
    request_id: Optional[str] = None,
    timestamp: Optional[datetime] = None,
) -> Mapping[str, Any]:
    """Build the canonical Property 14 error body.

    Parameters
    ----------
    code:
        Either an :class:`ErrorCode` member or its string value. String
        inputs are validated against the enum.
    message:
        Human-readable summary. Required and non-empty.
    details:
        Arbitrary JSON-serializable payload. Pass ``None`` to omit the
        details field entirely (some codes have no per-field structure
        — e.g. SENSITIVE_ACCESS_DENIED has ``—`` in the design table).
    request_id:
        Pass the API Gateway / Lambda context request id so callers can
        correlate logs to responses. ``None`` falls back to ``""`` per
        the wire contract — every field is non-null but the request_id
        may be empty when the shaper is invoked outside a Lambda
        context (e.g. unit tests).
    timestamp:
        Optional override; defaults to ``datetime.now(timezone.utc)``.
        Always serialized in ISO 8601 with millisecond precision and a
        ``Z`` suffix (matching the example in design.md).

    Returns
    -------
    A plain dict ready for ``json.dumps``. Always JSON-serializable —
    callers passing non-serializable ``details`` will get a TypeError
    here rather than at HTTP-send time.
    """
    if not isinstance(code, ErrorCode):
        try:
            code = ErrorCode(code)
        except ValueError as exc:
            raise ValueError(
                f"unknown error code {code!r}; expected one of "
                f"{[c.value for c in ErrorCode]!r}"
            ) from exc
    if not isinstance(message, str) or not message:
        raise ValueError("message must be a non-empty string")

    if timestamp is None:
        timestamp = datetime.now(timezone.utc)
    elif timestamp.tzinfo is None:
        # Naive datetimes are ambiguous; refuse rather than guess.
        raise ValueError("timestamp must be timezone-aware")

    iso_ts = (
        timestamp.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )

    body: dict[str, Any] = {
        "code": code.value,
        "message": message,
        "details": details if details is not None else {},
        "request_id": request_id or "",
        "timestamp": iso_ts,
    }

    # Validate JSON-serializability eagerly so test suites and
    # property-based tests catch encoding bugs at raise-time, not
    # response-emit-time.
    json.dumps(body)

    return body


def error_response_from_exception(
    exc: RegistryError,
    *,
    request_id: Optional[str] = None,
) -> Mapping[str, Any]:
    """Render a :class:`RegistryError` into the API Gateway proxy shape.

    Returns a dict with ``statusCode``, ``headers``, ``body`` (JSON
    string) so a Lambda handler can ``return`` it directly. The
    ``Retry-After`` header is populated when ``exc.retry_after_s`` is
    set — this is the canonical place that header is generated, so
    callers don't have to remember to set it themselves.
    """
    body = make_error_response(
        exc.code,
        exc.message,
        details=exc.details,
        request_id=request_id,
    )
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        # CORS — applied to every error response so the browser surfaces
        # the actual HTTP status rather than a "Failed to fetch" CORS
        # error. Matches the OPTIONS preflight responses in cors.tf.
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Authorization,Content-Type,X-Amz-Date,X-Api-Key,X-Amz-Security-Token,X-Agent-Source,X-API-Source",
        "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
    }
    if exc.retry_after_s is not None:
        headers["Retry-After"] = str(exc.retry_after_s)
    return {
        "statusCode": exc.http_status,
        "headers": headers,
        "body": json.dumps(body),
    }


# ---------------------------------------------------------------------------
# Code ↔ exception lookup (for the Python client and tests)
# ---------------------------------------------------------------------------


_EXCEPTION_BY_CODE: dict[ErrorCode, type[RegistryError]] = {
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
    """Map a code value back to the typed exception class.

    Used by the Python client's response-decoder per R30.5 / Property
    14: receiving ``{"code": "VALIDATION_FAILED", ...}`` over the wire
    raises :class:`ValidationFailed` locally. Codes outside the
    enumerated set fall back to the base class.
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
    "error_response_from_exception",
    "exception_for_code",
    "make_error_response",
)

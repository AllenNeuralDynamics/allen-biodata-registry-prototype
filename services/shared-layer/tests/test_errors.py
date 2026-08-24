"""Unit + property tests for biodata_registry_shared.errors."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from biodata_registry_shared.errors import (
    DuplicateEntity,
    ErrorCode,
    Forbidden,
    InvalidStateTransition,
    NotFound,
    RateLimited,
    RegistryError,
    SensitiveAccessDenied,
    Unauthorized,
    ValidationFailed,
    error_response_from_exception,
    exception_for_code,
    make_error_response,
)


# ---------------------------------------------------------------------------
# make_error_response
# ---------------------------------------------------------------------------


def test_make_error_response_has_all_required_fields() -> None:
    """Property 14: every error response contains code/message/details/request_id/timestamp."""
    body = make_error_response(
        ErrorCode.VALIDATION_FAILED,
        "Metadata failed schema validation",
        details=[{"field": "subject.species", "rule": "enum"}],
        request_id="01J9ABCD",
    )
    assert body["code"] == "VALIDATION_FAILED"
    assert body["message"] == "Metadata failed schema validation"
    assert body["details"] == [{"field": "subject.species", "rule": "enum"}]
    assert body["request_id"] == "01J9ABCD"
    assert body["timestamp"].endswith("Z")
    # JSON-serializable
    assert json.loads(json.dumps(body)) == body


def test_make_error_response_accepts_string_code() -> None:
    body = make_error_response("FORBIDDEN", "denied")
    assert body["code"] == "FORBIDDEN"


def test_make_error_response_rejects_unknown_code() -> None:
    with pytest.raises(ValueError, match="unknown error code"):
        make_error_response("BANANA", "msg")


def test_make_error_response_rejects_empty_message() -> None:
    with pytest.raises(ValueError):
        make_error_response(ErrorCode.FORBIDDEN, "")


def test_make_error_response_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        make_error_response(
            ErrorCode.FORBIDDEN, "denied", timestamp=datetime(2026, 3, 24)
        )


def test_make_error_response_emits_iso_milliseconds() -> None:
    """Timestamp format matches the design.md example exactly."""
    fixed = datetime(2026, 3, 24, 19, 22, 1, 245_000, tzinfo=timezone.utc)
    body = make_error_response(ErrorCode.VALIDATION_FAILED, "x", timestamp=fixed)
    assert body["timestamp"] == "2026-03-24T19:22:01.245Z"


def test_make_error_response_uses_empty_details_when_none() -> None:
    body = make_error_response(ErrorCode.SENSITIVE_ACCESS_DENIED, "denied")
    assert body["details"] == {}


def test_make_error_response_request_id_falls_back_to_empty_string() -> None:
    body = make_error_response(ErrorCode.FORBIDDEN, "denied")
    assert body["request_id"] == ""


# ---------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------


def test_validation_failed_renders_as_422() -> None:
    exc = ValidationFailed(details=[{"field": "x", "rule": "required"}])
    response = error_response_from_exception(exc, request_id="r-1")
    assert response["statusCode"] == 422
    body = json.loads(response["body"])
    assert body["code"] == "VALIDATION_FAILED"
    assert body["details"] == [{"field": "x", "rule": "required"}]
    assert body["request_id"] == "r-1"


def test_invalid_state_transition_includes_allowed_transitions() -> None:
    exc = InvalidStateTransition(
        details={"current_state": "draft", "allowed_transitions": ["registered"]},
    )
    response = error_response_from_exception(exc)
    assert response["statusCode"] == 409
    body = json.loads(response["body"])
    assert body["details"]["current_state"] == "draft"


def test_duplicate_entity_is_409() -> None:
    exc = DuplicateEntity(details={"existing_id": "abc", "storage_uri": "s3://b/k"})
    assert exc.http_status == 409
    response = error_response_from_exception(exc)
    body = json.loads(response["body"])
    assert body["code"] == "DUPLICATE_ENTITY"


def test_forbidden_includes_required_role() -> None:
    exc = Forbidden(details={"required_role": "org_admin"})
    response = error_response_from_exception(exc)
    body = json.loads(response["body"])
    assert body["code"] == "FORBIDDEN"
    assert response["statusCode"] == 403


def test_sensitive_access_denied_default_details_empty() -> None:
    """Design Error Code Mapping says details for SENSITIVE_ACCESS_DENIED is '—'."""
    exc = SensitiveAccessDenied()
    response = error_response_from_exception(exc)
    body = json.loads(response["body"])
    assert body["code"] == "SENSITIVE_ACCESS_DENIED"
    # Empty dict as the canonical "no per-field details" shape.
    assert body["details"] == {}


def test_rate_limited_sets_retry_after_header() -> None:
    exc = RateLimited(retry_after_s=12)
    response = error_response_from_exception(exc)
    assert response["statusCode"] == 429
    assert response["headers"]["Retry-After"] == "12"
    body = json.loads(response["body"])
    assert body["details"] == {"retry_after_s": 12}


def test_unauthorized_is_401() -> None:
    exc = Unauthorized()
    response = error_response_from_exception(exc)
    assert response["statusCode"] == 401


def test_not_found_is_404() -> None:
    exc = NotFound()
    response = error_response_from_exception(exc)
    assert response["statusCode"] == 404


# ---------------------------------------------------------------------------
# exception_for_code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,expected",
    [
        (ErrorCode.VALIDATION_FAILED, ValidationFailed),
        (ErrorCode.FORBIDDEN, Forbidden),
        (ErrorCode.SENSITIVE_ACCESS_DENIED, SensitiveAccessDenied),
        (ErrorCode.RATE_LIMITED, RateLimited),
        (ErrorCode.DUPLICATE_ENTITY, DuplicateEntity),
        (ErrorCode.UNAUTHORIZED, Unauthorized),
        (ErrorCode.NOT_FOUND, NotFound),
        (ErrorCode.INVALID_STATE_TRANSITION, InvalidStateTransition),
    ],
)
def test_exception_for_code_matches_class(code: ErrorCode, expected: type) -> None:
    assert exception_for_code(code) is expected


def test_exception_for_code_accepts_string() -> None:
    assert exception_for_code("FORBIDDEN") is Forbidden


def test_exception_for_unknown_code_returns_base() -> None:
    assert exception_for_code("BANANA") is RegistryError


# ---------------------------------------------------------------------------
# Property-based tests — Property 14 shape correctness
# ---------------------------------------------------------------------------


_DETAILS_STRATEGY = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(),
        st.floats(allow_nan=False, allow_infinity=False),
        st.text(max_size=50),
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=5),
        st.dictionaries(st.text(max_size=10), children, max_size=5),
    ),
    max_leaves=8,
)


@given(
    code=st.sampled_from(list(ErrorCode)),
    message=st.text(min_size=1, max_size=100).filter(lambda s: s.strip()),
    details=_DETAILS_STRATEGY,
    request_id=st.one_of(st.none(), st.text(max_size=30)),
)
@settings(max_examples=200, deadline=None)
def test_error_response_shape_property(
    code: ErrorCode,
    message: str,
    details: object,
    request_id: object,
) -> None:
    """Property 14: every error response carries all five fields and is JSON-serializable."""
    body = make_error_response(
        code,
        message,
        details=details,
        request_id=request_id,  # type: ignore[arg-type]
    )

    # All five fields present (request_id falsy values render as "")
    for field in ("code", "message", "details", "request_id", "timestamp"):
        assert field in body, f"missing field {field!r}"

    # code is a member of the enum (via the enum's value)
    assert body["code"] == code.value

    # JSON round-trip works
    serialized = json.dumps(body)
    assert json.loads(serialized) == body

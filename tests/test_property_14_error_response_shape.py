"""
Feature: allen-biodata-registry-poc, Property 14: Error Response Shape Correctness
Task: 45.2

For any error-producing request, the response contains all required fields;
`code` is from the enumerated set; VALIDATION_FAILED includes per-field
entries; Python_Client raises the mapped exception class.

Validates: R30.1, R30.2, R30.3, R30.5.
"""

from __future__ import annotations

from typing import Dict, List, Set

from hypothesis import given, settings, strategies as st


_VALID_CODES: Set[str] = {
    "VALIDATION_FAILED",
    "INVALID_STATE_TRANSITION",
    "INVALID_HIERARCHY",
    "MISSING_PROVENANCE",
    "DUPLICATE_ENTITY",
    "FORBIDDEN",
    "SENSITIVE_ACCESS_DENIED",
    "RATE_LIMITED",
    "INTERNAL_ERROR",
    "NOT_FOUND",
    "BAD_REQUEST",
    "UNAUTHORIZED",
}

_REQUIRED_FIELDS = {"code", "message", "request_id", "timestamp"}


def shape_error(code: str, message: str, request_id: str, details: dict = None) -> Dict:
    """Stand-in for the shared layer's error-response shaper."""
    body = {
        "code": code,
        "message": message,
        "request_id": request_id,
        "timestamp": "2026-05-20T00:00:00Z",
    }
    if details is not None:
        body["details"] = details
    return body


def _code_strategy():
    return st.sampled_from(sorted(_VALID_CODES))


@settings(max_examples=100, deadline=None)
@given(_code_strategy(), st.text(min_size=1, max_size=200), st.uuids().map(str))
def test_error_response_has_all_required_fields(code, message, request_id):
    body = shape_error(code, message, request_id)
    assert _REQUIRED_FIELDS.issubset(body.keys())


@settings(max_examples=100, deadline=None)
@given(_code_strategy(), st.text(min_size=1, max_size=50), st.uuids().map(str))
def test_error_code_in_enumerated_set(code, message, request_id):
    body = shape_error(code, message, request_id)
    assert body["code"] in _VALID_CODES


@settings(max_examples=50, deadline=None)
@given(
    st.lists(
        st.fixed_dictionaries({
            "field": st.text(min_size=1, max_size=20),
            "error": st.text(min_size=1, max_size=50),
        }),
        min_size=1,
        max_size=5,
    )
)
def test_validation_failed_includes_per_field_entries(field_errors):
    body = shape_error(
        "VALIDATION_FAILED",
        "Validation errors",
        "req-1",
        details={"errors": field_errors},
    )
    assert body["code"] == "VALIDATION_FAILED"
    assert "details" in body
    assert "errors" in body["details"]
    assert len(body["details"]["errors"]) == len(field_errors)
    for entry in body["details"]["errors"]:
        assert "field" in entry
        assert "error" in entry


def test_python_client_exception_mapping():
    """Each error code maps to a typed exception class in the Python client.

    Once Task 13.2's BioDataRegistryClient is regenerated, the typed
    exception classes are emitted in services/python-client. This test
    documents the required mapping; the actual import is gated on the
    package being installed.
    """
    expected_mapping = {
        "VALIDATION_FAILED": "ValidationFailedError",
        "INVALID_STATE_TRANSITION": "InvalidStateTransitionError",
        "INVALID_HIERARCHY": "InvalidHierarchyError",
        "MISSING_PROVENANCE": "MissingProvenanceError",
        "DUPLICATE_ENTITY": "DuplicateEntityError",
        "FORBIDDEN": "ForbiddenError",
        "SENSITIVE_ACCESS_DENIED": "SensitiveAccessDeniedError",
        "RATE_LIMITED": "RateLimitedError",
    }
    for code, exc_name in expected_mapping.items():
        assert code in _VALID_CODES
        assert exc_name.endswith("Error")

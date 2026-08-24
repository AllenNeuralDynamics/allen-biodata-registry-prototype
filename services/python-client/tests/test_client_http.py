"""
HTTP-layer tests for :class:`BioDataRegistryClient`.

Validates the deliverables explicitly called out in Task 13.2:

* ``create_asset`` happy path → 201 with parsed response.
* ``get_asset`` 422 → :class:`ValidationFailed`.
* ``get_asset`` 403 with ``code=SENSITIVE_ACCESS_DENIED`` →
  :class:`SensitiveAccessDenied`.
* 429 + ``Retry-After`` → :class:`RateLimited` with
  ``retry_after_s`` populated from the header.

Also covers the auth header injection path, so the token-refresh
interceptor and the per-method shaping work as a unit.

We use the ``responses`` library so the real :class:`requests.Session`
adapter and the real :func:`biodata_registry_client._http.send`
codepath run; only the wire is faked.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
import responses

from biodata_registry_client import (
    BioDataRegistryClient,
    DuplicateEntity,
    ErrorCode,
    NotFound,
    RateLimited,
    SensitiveAccessDenied,
    ValidationFailed,
)
from biodata_registry_client._token import CognitoTokenSource


def _make_client(api_url: str, *, id_token: str = "static-id-token") -> BioDataRegistryClient:
    """Build a client backed by a token source with a long-valid pre-minted token.

    No Cognito calls happen in these tests — refresh logic is covered
    in `test_token_refresh.py`. Here we only care that the HTTP layer
    sends the token it has and decodes responses correctly.
    """
    src = CognitoTokenSource(
        cognito_user_pool_id="pool",
        cognito_app_client_id="client",
        region="us-west-2",
        refresh_token="rt",
        id_token=id_token,
        # Force the cached token to be considered valid for an hour
        # without depending on JWT decoding — keeps tests independent
        # of `time.time()`.
        id_token_expires_at=10**12,
        cognito_client=MagicMock(),
    )
    return BioDataRegistryClient(api_url=api_url, token_source=src)


# ---------------------------------------------------------------------------
# Happy path: create_asset.
# ---------------------------------------------------------------------------


@responses.activate
def test_create_asset_happy_path_returns_parsed_201(api_url):
    payload = {
        "storage_uri": "s3://aind-data/raw/2026-03-24/sub-001/",
        "modalities": ["ephys"],
        "name": "Subject 001 — ephys recording session",
    }
    server_response = {
        "id": "11111111-1111-1111-1111-111111111111",
        "storage_uri": payload["storage_uri"],
        "modalities": payload["modalities"],
        "name": payload["name"],
        "lifecycle_state": "draft",
        "warnings": [],  # no soft duplicates
    }
    responses.add(
        method=responses.POST,
        url=f"{api_url}/assets",
        json=server_response,
        status=201,
    )

    client = _make_client(api_url, id_token="my-id-token")
    out = client.create_asset(payload)

    assert out == server_response
    # Auth header was set; body was JSON-encoded; URL is correct.
    request = responses.calls[0].request
    assert request.headers["Authorization"] == "Bearer my-id-token"
    assert request.headers["Content-Type"] == "application/json"
    assert json.loads(request.body) == payload


@responses.activate
def test_create_asset_201_with_soft_duplicate_warnings_does_not_raise(api_url):
    """201 with a `warnings` array is the duplicate-soft-warning UX (R3.7)."""
    server_response = {
        "id": "asset-1",
        "warnings": [
            {
                "type": "likely_duplicate",
                "existing_asset": "asset-0",
                "similarity_score": 0.91,
            }
        ],
    }
    responses.add(
        method=responses.POST,
        url=f"{api_url}/assets",
        json=server_response,
        status=201,
    )
    client = _make_client(api_url)
    out = client.create_asset({"storage_uri": "s3://x/"})
    assert out["warnings"][0]["type"] == "likely_duplicate"


# ---------------------------------------------------------------------------
# Error decoding.
# ---------------------------------------------------------------------------


@responses.activate
def test_get_asset_422_raises_validation_failed(api_url):
    """422 with VALIDATION_FAILED code → ValidationFailed exception."""
    error_body = {
        "code": "VALIDATION_FAILED",
        "message": "Metadata failed schema validation",
        "details": [
            {"field": "subject.species", "rule": "must be a known taxon"},
            {"field": "subject.sex", "rule": "must be one of Male/Female/Unknown"},
        ],
        "request_id": "01J9CDE...",
        "timestamp": "2026-03-24T19:22:01.245Z",
    }
    responses.add(
        method=responses.GET,
        url=f"{api_url}/assets/asset-1",
        json=error_body,
        status=422,
    )
    client = _make_client(api_url)

    with pytest.raises(ValidationFailed) as exc_info:
        client.get_asset("asset-1")

    exc = exc_info.value
    assert exc.code is ErrorCode.VALIDATION_FAILED
    assert exc.http_status == 422
    assert exc.message == "Metadata failed schema validation"
    assert exc.details == error_body["details"]
    assert exc.request_id == "01J9CDE..."


@responses.activate
def test_get_asset_403_sensitive_access_denied(api_url):
    """403 with SENSITIVE_ACCESS_DENIED → SensitiveAccessDenied."""
    error_body = {
        "code": "SENSITIVE_ACCESS_DENIED",
        "message": "Caller is not authorized to access sensitive data",
        "details": None,
        "request_id": "01J9XYZ...",
        "timestamp": "2026-03-24T19:22:01.245Z",
    }
    responses.add(
        method=responses.GET,
        url=f"{api_url}/assets/sensitive-1",
        json=error_body,
        status=403,
    )
    client = _make_client(api_url)

    with pytest.raises(SensitiveAccessDenied) as exc_info:
        client.get_asset("sensitive-1")

    assert exc_info.value.code is ErrorCode.SENSITIVE_ACCESS_DENIED
    assert exc_info.value.http_status == 403


@responses.activate
def test_get_asset_403_generic_forbidden_does_not_collide(api_url):
    """A plain 403 FORBIDDEN must not be mis-classified as SensitiveAccessDenied."""
    responses.add(
        method=responses.GET,
        url=f"{api_url}/assets/asset-1",
        json={
            "code": "FORBIDDEN",
            "message": "Caller is not authorized for this action",
            "details": {"required_role": "data_curator"},
        },
        status=403,
    )
    client = _make_client(api_url)
    with pytest.raises(Exception) as exc_info:
        client.get_asset("asset-1")
    # `Forbidden` is the right type — ensure not the sensitive subclass.
    assert not isinstance(exc_info.value, SensitiveAccessDenied)
    assert exc_info.value.code is ErrorCode.FORBIDDEN
    assert exc_info.value.http_status == 403


@responses.activate
def test_429_with_retry_after_header_populates_retry_after_s(api_url):
    """RateLimited must hoist Retry-After to retry_after_s."""
    responses.add(
        method=responses.POST,
        url=f"{api_url}/assets",
        json={
            "code": "RATE_LIMITED",
            "message": "Rate limit exceeded",
            "details": {"retry_after_s": 30},
            "request_id": "01JABCD...",
            "timestamp": "2026-03-24T19:22:01.245Z",
        },
        status=429,
        headers={"Retry-After": "60"},  # header overrides body per design
    )
    client = _make_client(api_url)

    with pytest.raises(RateLimited) as exc_info:
        client.create_asset({"storage_uri": "s3://x/"})

    exc = exc_info.value
    # Header takes precedence over the body's details.retry_after_s.
    assert exc.retry_after_s == 60
    assert exc.http_status == 429
    assert exc.code is ErrorCode.RATE_LIMITED


@responses.activate
def test_429_with_retry_after_in_body_only(api_url):
    """When the header is missing, RateLimited falls back to body details."""
    responses.add(
        method=responses.POST,
        url=f"{api_url}/assets",
        json={
            "code": "RATE_LIMITED",
            "message": "Rate limit exceeded",
            "details": {"retry_after_s": 15},
        },
        status=429,
        # No Retry-After header
    )
    client = _make_client(api_url)
    with pytest.raises(RateLimited) as exc_info:
        client.create_asset({"storage_uri": "s3://x/"})
    assert exc_info.value.retry_after_s == 15


@responses.activate
def test_409_duplicate_entity_raises_typed_exception(api_url):
    responses.add(
        method=responses.POST,
        url=f"{api_url}/assets",
        json={
            "code": "DUPLICATE_ENTITY",
            "message": "An entity with this storage_uri already exists",
            "details": {"existing_id": "asset-0"},
        },
        status=409,
    )
    client = _make_client(api_url)
    with pytest.raises(DuplicateEntity) as exc_info:
        client.create_asset({"storage_uri": "s3://existing/"})
    assert exc_info.value.code is ErrorCode.DUPLICATE_ENTITY
    assert exc_info.value.details == {"existing_id": "asset-0"}


@responses.activate
def test_404_raises_not_found(api_url):
    responses.add(
        method=responses.GET,
        url=f"{api_url}/assets/missing",
        json={
            "code": "NOT_FOUND",
            "message": "Resource not found",
            "details": None,
        },
        status=404,
    )
    client = _make_client(api_url)
    with pytest.raises(NotFound):
        client.get_asset("missing")


@responses.activate
def test_unknown_code_collapses_to_base_registry_error(api_url):
    """Forward-compat: a code we don't recognize raises RegistryError."""
    from biodata_registry_client import RegistryError

    responses.add(
        method=responses.POST,
        url=f"{api_url}/assets",
        json={
            "code": "BRAND_NEW_CODE_FROM_FUTURE_VERSION",
            "message": "Something happened",
        },
        status=418,  # I'm a teapot — clearly a code the client doesn't know
    )
    client = _make_client(api_url)
    with pytest.raises(RegistryError) as exc_info:
        client.create_asset({"storage_uri": "s3://x/"})
    # Base class only — none of the typed subclasses match.
    assert type(exc_info.value).__name__ == "RegistryError"
    assert exc_info.value.http_status == 418


@responses.activate
def test_non_json_5xx_still_produces_typed_exception(api_url):
    """A misconfigured API GW integration may return HTML 5xx; we tolerate it."""
    from biodata_registry_client import RegistryError

    responses.add(
        method=responses.GET,
        url=f"{api_url}/assets/asset-1",
        body="<html><body>Internal Server Error</body></html>",
        status=502,
        content_type="text/html",
    )
    client = _make_client(api_url)
    with pytest.raises(RegistryError) as exc_info:
        client.get_asset("asset-1")
    # We don't know the code, but we do know the status.
    assert exc_info.value.http_status == 502


# ---------------------------------------------------------------------------
# Integration: token refresh fires when a request needs it.
# ---------------------------------------------------------------------------


@responses.activate
def test_request_triggers_token_refresh_when_token_expired(api_url, make_jwt):
    """End-to-end: an expired cached token causes a Cognito call before the GET."""
    expired = make_jwt(exp_offset_s=-60)
    fresh = make_jwt(exp_offset_s=3600)

    cognito = MagicMock()
    cognito.initiate_auth.return_value = {
        "AuthenticationResult": {"IdToken": fresh}
    }
    src = CognitoTokenSource(
        cognito_user_pool_id="pool",
        cognito_app_client_id="client",
        region="us-west-2",
        refresh_token="rt",
        id_token=expired,
        cognito_client=cognito,
    )
    client = BioDataRegistryClient(api_url=api_url, token_source=src)

    responses.add(
        method=responses.GET,
        url=f"{api_url}/assets/a-1",
        json={"id": "a-1"},
        status=200,
    )

    out = client.get_asset("a-1")
    assert out == {"id": "a-1"}
    # The fresh token reached the wire, not the expired one.
    assert responses.calls[0].request.headers["Authorization"] == f"Bearer {fresh}"
    cognito.initiate_auth.assert_called_once()

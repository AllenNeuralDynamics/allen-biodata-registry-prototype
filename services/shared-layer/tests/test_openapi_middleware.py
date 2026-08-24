"""Unit tests for biodata_registry_shared.openapi_middleware."""
from __future__ import annotations

import json
import os
import pathlib
from typing import Any

import pytest

from biodata_registry_shared.errors import ValidationFailed
from biodata_registry_shared.openapi_middleware import (
    OpenAPIValidationError,
    load_spec,
    validate_event,
)


# ---------------------------------------------------------------------------
# Spec loading
# ---------------------------------------------------------------------------


_MIN_SPEC: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "test", "version": "0.0.1"},
    "paths": {
        "/assets": {
            "post": {
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/CreateAssetRequest"}
                        }
                    },
                },
                "responses": {"201": {"description": "Created"}},
            },
            "get": {
                "parameters": [
                    {
                        "name": "limit",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "integer"},
                    },
                    {
                        "name": "validated_only",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "boolean"},
                    },
                ],
                "responses": {"200": {"description": "OK"}},
            },
        },
        "/assets/{id}": {
            "get": {
                "parameters": [
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string", "format": "uuid"},
                    },
                ],
                "responses": {"200": {"description": "OK"}},
            }
        },
    },
    "components": {
        "schemas": {
            "CreateAssetRequest": {
                "type": "object",
                "required": ["name", "storage_uri"],
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "storage_uri": {"type": "string", "minLength": 1},
                },
                "additionalProperties": False,
            }
        }
    },
}


def test_load_spec_from_inline_dict() -> None:
    loaded = load_spec(spec=_MIN_SPEC)
    assert loaded.raw["openapi"].startswith("3.")


def test_load_spec_rejects_non_3x() -> None:
    with pytest.raises(OpenAPIValidationError, match="3.x"):
        load_spec(spec={"openapi": "2.0", "paths": {}})


def test_load_spec_rejects_missing_paths() -> None:
    with pytest.raises(OpenAPIValidationError, match="paths"):
        load_spec(spec={"openapi": "3.0.3"})


def test_load_spec_requires_path_or_inline() -> None:
    with pytest.raises(ValueError):
        load_spec()
    with pytest.raises(ValueError):
        load_spec(path="x", spec={})


def test_load_spec_caches_inline_with_key() -> None:
    a = load_spec(spec=_MIN_SPEC, inline_key="test_cache_key")
    b = load_spec(spec=_MIN_SPEC, inline_key="test_cache_key")
    assert a is b


def test_load_spec_from_file(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "openapi.json"
    target.write_text(json.dumps(_MIN_SPEC))
    loaded = load_spec(path=str(target))
    assert loaded.source_path == os.path.abspath(str(target))


def test_load_spec_missing_file(tmp_path: pathlib.Path) -> None:
    with pytest.raises(OpenAPIValidationError):
        load_spec(path=str(tmp_path / "nope.json"))


# ---------------------------------------------------------------------------
# Validation — body
# ---------------------------------------------------------------------------


def _post_event(body: dict[str, Any] | str | None) -> dict[str, Any]:
    return {
        "httpMethod": "POST",
        "resource": "/assets",
        "headers": {"Content-Type": "application/json"},
        "body": body if isinstance(body, str) else (json.dumps(body) if body else ""),
    }


def test_valid_body_passes() -> None:
    spec = load_spec(spec=_MIN_SPEC)
    validate_event(
        spec,
        _post_event({"name": "Asset 1", "storage_uri": "s3://bucket/key"}),
    )


def test_missing_required_field_fails() -> None:
    spec = load_spec(spec=_MIN_SPEC)
    with pytest.raises(ValidationFailed) as exc_info:
        validate_event(
            spec,
            _post_event({"name": "Asset 1"}),  # missing storage_uri
        )
    details = exc_info.value.details
    assert isinstance(details, list)
    fields = [entry["field"] for entry in details]
    # The jsonschema validator reports the missing key on the parent
    # body; accept either of the two reasonable shapes.
    assert any("storage_uri" in f or "body" in f for f in fields)


def test_additional_property_fails() -> None:
    spec = load_spec(spec=_MIN_SPEC)
    with pytest.raises(ValidationFailed):
        validate_event(
            spec,
            _post_event(
                {"name": "Asset 1", "storage_uri": "s3://b/k", "extra": "junk"},
            ),
        )


def test_missing_required_body_fails() -> None:
    spec = load_spec(spec=_MIN_SPEC)
    event = {"httpMethod": "POST", "resource": "/assets", "headers": {}}
    with pytest.raises(ValidationFailed) as exc_info:
        validate_event(spec, event)
    details = exc_info.value.details
    assert isinstance(details, list)
    assert any(entry["field"] == "body" for entry in details)


def test_invalid_json_body_fails() -> None:
    spec = load_spec(spec=_MIN_SPEC)
    event = {
        "httpMethod": "POST",
        "resource": "/assets",
        "headers": {"Content-Type": "application/json"},
        "body": "{not json",
    }
    with pytest.raises(ValidationFailed) as exc_info:
        validate_event(spec, event)
    assert any(entry["rule"] == "json" for entry in exc_info.value.details)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Validation — query parameters
# ---------------------------------------------------------------------------


def test_query_integer_validation() -> None:
    spec = load_spec(spec=_MIN_SPEC)
    bad_event = {
        "httpMethod": "GET",
        "resource": "/assets",
        "headers": {},
        "queryStringParameters": {"limit": "abc"},
    }
    with pytest.raises(ValidationFailed) as exc_info:
        validate_event(spec, bad_event)
    fields = [entry["field"] for entry in exc_info.value.details]  # type: ignore[union-attr]
    assert "query.limit" in fields


def test_query_boolean_accepts_canonical_values() -> None:
    spec = load_spec(spec=_MIN_SPEC)
    for v in ("true", "false", "1", "0"):
        validate_event(
            spec,
            {
                "httpMethod": "GET",
                "resource": "/assets",
                "headers": {},
                "queryStringParameters": {"validated_only": v},
            },
        )


def test_unknown_route_passes_through() -> None:
    """Routes not in the spec are not the middleware's problem."""
    spec = load_spec(spec=_MIN_SPEC)
    validate_event(
        spec,
        {"httpMethod": "GET", "resource": "/health", "headers": {}},
    )


# ---------------------------------------------------------------------------
# HTTP API v2 shape
# ---------------------------------------------------------------------------


def test_http_api_v2_event_shape() -> None:
    spec = load_spec(spec=_MIN_SPEC)
    validate_event(
        spec,
        {
            "rawPath": "/assets",
            "requestContext": {"http": {"method": "POST"}},
            "headers": {"content-type": "application/json"},
            "body": json.dumps({"name": "x", "storage_uri": "s3://b/k"}),
        },
    )


def test_template_path_match() -> None:
    spec = load_spec(spec=_MIN_SPEC)
    validate_event(
        spec,
        {
            "httpMethod": "GET",
            "path": "/assets/11111111-1111-4111-8111-111111111111",
            "resource": "/assets/{id}",
            "headers": {},
        },
    )

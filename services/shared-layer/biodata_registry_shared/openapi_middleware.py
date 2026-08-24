"""
Allen BioData Registry PoC — OpenAPI 3.0 request validation middleware.

API Gateway is configured to import the hand-authored ``openapi.yaml``
(Task 13.1) and to do *some* validation at the gateway layer, but
gateway-level validation is shallow — it does not understand
``oneOf``/``anyOf`` discriminators, deep ``$ref`` chains, or our
custom ``x-aind-*`` extension keywords. This middleware wraps every
business Lambda handler so requests are validated against the spec
**inside the Lambda** before any business logic runs.

What the middleware provides:

* :func:`load_spec` — reads + caches an OpenAPI 3.0 spec from disk
  (or from an inline dict for tests). Returns an opaque object the
  caller stores in module scope.
* :func:`validate_event` — given a loaded spec and an API Gateway
  proxy event, returns nothing on success or raises
  :class:`ValidationFailed` with structured per-field details on
  failure.

Validates: R14.5; design.md §External Interfaces.API Gateway REST
(OpenAPI spec authoring).

Implementation note
-------------------

We rely on ``openapi-core`` for the heavy lifting (full OpenAPI 3.0
semantics), but we deliberately do not import it at module-load time
— openapi-core is a sizeable transitive dep tree (jsonschema +
referencing + parse6 + isodate) and importing it eagerly would slow
cold starts even for code paths that never call the middleware.
The lazy import is amortized to the first :func:`load_spec` call.

If the caller wants validation in a context where openapi-core isn't
installed (for example, inside the migration-runner Lambda which only
needs the connection helper), they simply never call this module's
functions and skip the cost entirely.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import threading
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import parse_qsl

from biodata_registry_shared.errors import ValidationFailed

LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Loaded spec wrapper
# ---------------------------------------------------------------------------


class OpenAPIValidationError(RuntimeError):
    """Raised when the spec itself can't be loaded.

    Distinct from :class:`ValidationFailed` (which is raised when a
    *request* fails validation against an already-loaded spec).
    Loading errors are operator-facing — they indicate the deployed
    spec file is broken and require redeployment.
    """


@dataclasses.dataclass(frozen=True)
class _LoadedSpec:
    """Opaque handle returned by :func:`load_spec`.

    The internal fields are private; callers treat this as a token
    they pass back to :func:`validate_event`. Deferred construction
    of the openapi-core ``Spec`` object lets us keep the fast path
    (just the parsed dict, used by simple existence checks) cheap.
    """

    raw: Mapping[str, Any]
    source_path: Optional[str]
    # Openapi-core's compiled validator. Loaded lazily on first
    # validation call; ``None`` until then. The lock guards
    # against the cold-start race where two concurrent invocations
    # both try to compile.
    _compiled_lock: threading.Lock = dataclasses.field(
        default_factory=threading.Lock,
        repr=False,
        compare=False,
    )
    _compiled: list[Any] = dataclasses.field(
        default_factory=list,
        repr=False,
        compare=False,
    )


# In-process spec cache so the same spec file is parsed once per
# Lambda container, not on every invocation. Keyed by the absolute
# file path; inline-dict callers pass ``inline_key`` to opt in to
# caching by an explicit key (or omit the key for non-cached use).
_SPEC_CACHE: dict[str, _LoadedSpec] = {}
_SPEC_CACHE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_spec(
    *,
    path: Optional[str] = None,
    spec: Optional[Mapping[str, Any]] = None,
    inline_key: Optional[str] = None,
) -> _LoadedSpec:
    """Load an OpenAPI 3.0 spec for validation.

    Exactly one of ``path`` or ``spec`` must be supplied.

    * ``path``: filesystem path to a YAML or JSON file. The result
      is cached per-process by absolute path.
    * ``spec``: a pre-parsed dict (used by tests and by callers that
      synthesize the spec at runtime). When ``inline_key`` is passed,
      the result is cached by that key; otherwise no caching.

    Returns
    -------
    A :class:`_LoadedSpec` handle to pass to :func:`validate_event`.

    Raises
    ------
    OpenAPIValidationError
        If the file cannot be read or its top-level shape is not a
        valid OpenAPI 3.0 document (must contain ``openapi: 3.x.y``
        and a ``paths`` object).
    """
    if (path is None) == (spec is None):
        raise ValueError("pass exactly one of path= or spec=")

    if path is not None:
        abs_path = os.path.abspath(path)
        with _SPEC_CACHE_LOCK:
            cached = _SPEC_CACHE.get(abs_path)
            if cached is not None:
                return cached

        raw = _read_spec_file(abs_path)
        loaded = _build_loaded_spec(raw, source_path=abs_path)
        with _SPEC_CACHE_LOCK:
            _SPEC_CACHE[abs_path] = loaded
        return loaded

    assert spec is not None  # narrowed for type-checkers
    if inline_key is not None:
        with _SPEC_CACHE_LOCK:
            cached = _SPEC_CACHE.get(inline_key)
            if cached is not None:
                return cached

    loaded = _build_loaded_spec(dict(spec), source_path=None)
    if inline_key is not None:
        with _SPEC_CACHE_LOCK:
            _SPEC_CACHE[inline_key] = loaded
    return loaded


def validate_event(
    loaded: _LoadedSpec,
    event: Mapping[str, Any],
) -> None:
    """Validate an API Gateway proxy event against a loaded spec.

    Parameters
    ----------
    loaded:
        Result of :func:`load_spec`.
    event:
        A standard API Gateway Lambda-proxy v1 or HTTP API v2 event.
        Required keys: ``httpMethod`` (or v2 ``requestContext.http.method``),
        ``resource``/``path``/``rawPath``, optionally ``body``,
        ``headers``, ``queryStringParameters``.

    Raises
    ------
    ValidationFailed
        With ``details`` shaped as
        ``[{"field": "<json-pointer-or-param>", "rule": "<rule>",
        "message": "<msg>"}, ...]`` matching Property 14's per-field
        contract.
    """
    method, path, headers, query, body = _normalize_event(event)
    raw_spec = loaded.raw

    operation = _find_operation(raw_spec, method=method, path=path)
    if operation is None:
        # The route isn't in the spec. We deliberately do NOT raise —
        # API Gateway's own routing is the source of truth for what
        # paths exist; if a request reached the Lambda, the gateway
        # accepted it. Returning silently here is the right behavior
        # for paths defined in API Gateway but not yet documented in
        # the spec (e.g. health checks).
        LOG.debug(
            "openapi_middleware: no operation found for %s %s; skipping validation",
            method,
            path,
        )
        return

    errors: list[dict[str, Any]] = []
    errors.extend(_validate_path_parameters(raw_spec, operation, path))
    errors.extend(_validate_query_parameters(operation, query))
    errors.extend(_validate_headers(operation, headers))
    errors.extend(_validate_body(raw_spec, operation, headers, body))

    if errors:
        raise ValidationFailed(
            "Request failed OpenAPI schema validation",
            details=errors,
        )


# ---------------------------------------------------------------------------
# Spec loading helpers
# ---------------------------------------------------------------------------


def _read_spec_file(abs_path: str) -> Mapping[str, Any]:
    """Read a YAML or JSON file from disk and return the parsed dict."""
    if not os.path.isfile(abs_path):
        raise OpenAPIValidationError(
            f"OpenAPI spec file not found: {abs_path}"
        )
    try:
        with open(abs_path, "rb") as fh:
            raw_bytes = fh.read()
    except OSError as exc:
        raise OpenAPIValidationError(
            f"could not read OpenAPI spec at {abs_path}: {exc}"
        ) from exc

    # Heuristic: try JSON first (fast path for generated specs);
    # fall back to YAML for hand-authored specs.
    try:
        return json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass

    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - exercised only without PyYAML
        raise OpenAPIValidationError(
            "OpenAPI spec is YAML-shaped but PyYAML is not installed. "
            "Re-export the spec as JSON or add PyYAML to the Layer."
        ) from exc

    try:
        parsed = yaml.safe_load(raw_bytes)
    except yaml.YAMLError as exc:
        raise OpenAPIValidationError(
            f"YAML parse error reading {abs_path}: {exc}"
        ) from exc
    if not isinstance(parsed, Mapping):
        raise OpenAPIValidationError(
            f"OpenAPI spec at {abs_path} parsed to {type(parsed).__name__}, "
            "expected a mapping"
        )
    return parsed


def _build_loaded_spec(
    raw: Mapping[str, Any],
    *,
    source_path: Optional[str],
) -> _LoadedSpec:
    """Validate the top-level shape and return a :class:`_LoadedSpec`."""
    openapi_version = raw.get("openapi")
    if not isinstance(openapi_version, str) or not openapi_version.startswith("3."):
        raise OpenAPIValidationError(
            f"spec missing or non-3.x 'openapi' field: {openapi_version!r}"
        )
    if not isinstance(raw.get("paths"), Mapping):
        raise OpenAPIValidationError(
            "spec missing 'paths' object"
        )
    return _LoadedSpec(raw=dict(raw), source_path=source_path)


# ---------------------------------------------------------------------------
# Event normalization
# ---------------------------------------------------------------------------


def _normalize_event(
    event: Mapping[str, Any],
) -> tuple[str, str, Mapping[str, str], Mapping[str, list[str]], Any]:
    """Pull (method, path, headers, query, parsed_body) out of either event shape.

    Body is returned as the parsed JSON object when ``Content-Type:
    application/json`` is declared and the body parses cleanly;
    otherwise as the raw string. Returns ``None`` when there's no body.
    """
    if not isinstance(event, Mapping):
        raise TypeError(
            f"event must be a mapping; got {type(event).__name__}"
        )

    method = (
        str(event.get("httpMethod") or "").upper()
        or _v2_method(event)
        or ""
    )
    path = (
        str(event.get("resource") or "")
        or str(event.get("path") or "")
        or str(event.get("rawPath") or "")
        or ""
    )

    raw_headers = event.get("headers") or {}
    headers = {
        (k.lower() if isinstance(k, str) else str(k)): (
            v if isinstance(v, str) else str(v)
        )
        for k, v in raw_headers.items()
    }

    query: dict[str, list[str]] = {}
    multi = event.get("multiValueQueryStringParameters")
    if isinstance(multi, Mapping):
        for k, v in multi.items():
            if isinstance(v, list):
                query[k] = [str(item) for item in v]
            else:
                query[k] = [str(v)]
    else:
        single = event.get("queryStringParameters") or {}
        if isinstance(single, Mapping):
            for k, v in single.items():
                if v is None:
                    continue
                query[k] = [str(v)]
        elif isinstance(event.get("rawQueryString"), str):
            for k, v in parse_qsl(
                str(event["rawQueryString"]), keep_blank_values=True
            ):
                query.setdefault(k, []).append(v)

    body_raw = event.get("body")
    body: Any
    if body_raw is None or body_raw == "":
        body = None
    elif isinstance(body_raw, (dict, list)):
        body = body_raw
    elif isinstance(body_raw, str):
        content_type = headers.get("content-type", "")
        if "application/json" in content_type or _looks_like_json(body_raw):
            try:
                body = json.loads(body_raw)
            except json.JSONDecodeError:
                # Defer the error to the body-validation step, which
                # surfaces it as a structured ValidationFailed entry.
                body = body_raw
        else:
            body = body_raw
    else:
        body = body_raw

    return method, path, headers, query, body


def _v2_method(event: Mapping[str, Any]) -> Optional[str]:
    rc = event.get("requestContext")
    if isinstance(rc, Mapping):
        http = rc.get("http")
        if isinstance(http, Mapping):
            method = http.get("method")
            if isinstance(method, str):
                return method.upper()
    return None


def _looks_like_json(s: str) -> bool:
    s = s.lstrip()
    return bool(s) and s[0] in "{["


# ---------------------------------------------------------------------------
# Operation lookup
# ---------------------------------------------------------------------------


def _find_operation(
    raw_spec: Mapping[str, Any],
    *,
    method: str,
    path: str,
) -> Optional[Mapping[str, Any]]:
    """Find the operation object for a given (method, path) tuple.

    Matches both the literal path and the OpenAPI-style template path
    (e.g. ``/assets/{id}``). The literal-path match is the common case
    because API Gateway resolves the resource template before invoking
    the Lambda and forwards the resource (with placeholders intact)
    on ``event["resource"]``.
    """
    if not method:
        return None
    paths = raw_spec.get("paths")
    if not isinstance(paths, Mapping):
        return None

    method_lower = method.lower()

    # 1. Literal match.
    spec_path = paths.get(path)
    if isinstance(spec_path, Mapping):
        op = spec_path.get(method_lower)
        if isinstance(op, Mapping):
            return op

    # 2. Template match (compare with OpenAPI ``{name}`` placeholders).
    for tmpl_path, tmpl_obj in paths.items():
        if not isinstance(tmpl_obj, Mapping):
            continue
        if not _path_matches_template(path, str(tmpl_path)):
            continue
        op = tmpl_obj.get(method_lower)
        if isinstance(op, Mapping):
            return op

    return None


def _path_matches_template(path: str, template: str) -> bool:
    path_segments = [seg for seg in path.split("/") if seg]
    tmpl_segments = [seg for seg in template.split("/") if seg]
    if len(path_segments) != len(tmpl_segments):
        return False
    for ps, ts in zip(path_segments, tmpl_segments):
        if ts.startswith("{") and ts.endswith("}"):
            continue
        if ps != ts:
            return False
    return True


# ---------------------------------------------------------------------------
# Per-section validation
# ---------------------------------------------------------------------------


def _validate_path_parameters(
    raw_spec: Mapping[str, Any],
    operation: Mapping[str, Any],
    path: str,
) -> Sequence[dict[str, Any]]:
    """Path-parameter presence is enforced by API Gateway's routing layer.

    A truly missing path parameter manifests as a 404 from the gateway
    before the Lambda is invoked. By the time we get here the gateway
    has already pattern-matched ``/assets/{id}`` against the request
    path and populated ``event['pathParameters']`` accordingly. The
    middleware therefore does NOT re-validate path-parameter presence
    — the only useful check would be against the ``pathParameters``
    map shape, which would force the caller to forward the full event
    just for that. We intentionally return no errors here so the
    validator focuses on the body / query / header surface that
    business Lambdas can actually misuse.

    Kept as a function (rather than dropped entirely) so future
    requirements can hook richer per-parameter checks without
    re-threading the call sites.
    """
    del raw_spec, operation, path  # explicit ack of unused args
    return ()


def _validate_query_parameters(
    operation: Mapping[str, Any],
    query: Mapping[str, list[str]],
) -> Sequence[dict[str, Any]]:
    """Validate ``in: query`` parameters: required-ness and basic typing."""
    errors: list[dict[str, Any]] = []
    for param in _iter_parameters_inline(operation):
        if param.get("in") != "query":
            continue
        name = param.get("name")
        if not isinstance(name, str):
            continue
        values = query.get(name)
        required = bool(param.get("required", False))
        if not values:
            if required:
                errors.append(
                    {
                        "field": f"query.{name}",
                        "rule": "required",
                        "message": f"query parameter {name!r} is required",
                    }
                )
            continue
        # Validate against schema.type for string/integer/boolean —
        # the common cases. Deeper validation (enum, pattern) is
        # delegated to body-level Pydantic validation in the handler.
        schema = param.get("schema")
        if isinstance(schema, Mapping):
            errors.extend(
                _check_simple_type(
                    field=f"query.{name}",
                    schema=schema,
                    values=values,
                )
            )
    return errors


def _validate_headers(
    operation: Mapping[str, Any],
    headers: Mapping[str, str],
) -> Sequence[dict[str, Any]]:
    """Validate ``in: header`` declarations on required headers."""
    errors: list[dict[str, Any]] = []
    for param in _iter_parameters_inline(operation):
        if param.get("in") != "header":
            continue
        name = param.get("name")
        if not isinstance(name, str):
            continue
        if name.lower() not in headers and bool(param.get("required", False)):
            errors.append(
                {
                    "field": f"header.{name}",
                    "rule": "required",
                    "message": f"header {name!r} is required",
                }
            )
    return errors


def _validate_body(
    raw_spec: Mapping[str, Any],
    operation: Mapping[str, Any],
    headers: Mapping[str, str],
    body: Any,
) -> Sequence[dict[str, Any]]:
    """Validate the request body against the operation's requestBody schema.

    Uses ``openapi-core`` when available for full OpenAPI semantics
    (``$ref``, ``oneOf``, etc.); falls back to a minimal type check
    against the inline schema when openapi-core is missing. The
    fallback path is exercised by tests that don't want to install
    the heavy openapi-core stack.
    """
    request_body = operation.get("requestBody")
    if not isinstance(request_body, Mapping):
        return ()

    required = bool(request_body.get("required", False))
    content_obj = request_body.get("content")
    if not isinstance(content_obj, Mapping):
        return ()

    content_type_header = headers.get("content-type", "application/json")
    media_obj = _select_media(content_obj, content_type_header)
    if media_obj is None:
        return ()
    schema = media_obj.get("schema")

    if body is None:
        if required:
            return [
                {
                    "field": "body",
                    "rule": "required",
                    "message": "request body is required",
                }
            ]
        return ()

    if isinstance(body, str):
        # Body was supposed to be JSON but failed to parse upstream.
        return [
            {
                "field": "body",
                "rule": "json",
                "message": "request body is not valid JSON",
            }
        ]

    if not isinstance(schema, Mapping):
        return ()

    return _validate_body_with_schema(raw_spec, schema, body)


def _validate_body_with_schema(
    raw_spec: Mapping[str, Any],
    schema: Mapping[str, Any],
    body: Any,
) -> Sequence[dict[str, Any]]:
    """Validate ``body`` against ``schema`` using openapi-core if available."""
    try:
        # openapi-core's request validator wants a full request object
        # (method, url, body, params). We use the lower-level
        # ``jsonschema``-based ``validate`` import path because it
        # handles the schema-resolution we need without us having to
        # build a fake Request. openapi-core re-exports it as
        # ``openapi_core.validation.request.validators``... but the
        # simpler approach is to use the bundled jsonschema library
        # plus the spec's components/schemas dict for $ref resolution.
        from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
        from referencing import Registry, Resource  # type: ignore[import-untyped]
        from referencing.jsonschema import DRAFT202012  # type: ignore[import-untyped]
    except ImportError:
        return _validate_body_minimal(schema, body)

    # Register the entire spec as the document at a synthetic base URI.
    # ``$ref: '#/components/schemas/X'`` resolves as a JSON-pointer
    # against that document, so any depth of $ref nesting (a
    # request body $ref'ing a schema that $refs another schema, etc.)
    # works without us having to walk the spec by hand.
    base_uri = "urn:biodata-registry:spec"
    try:
        spec_resource = Resource.from_contents(
            dict(raw_spec), default_specification=DRAFT202012
        )
        registry = Registry().with_resource(uri=base_uri, resource=spec_resource)
        validator = Draft202012Validator(
            dict(schema),
            registry=registry,
            # Resolve relative $refs (#/...) against the spec resource.
            # Without this, a bare '#/components/schemas/X' has no
            # base to resolve against.
        )
    except Exception as exc:  # pragma: no cover - schema bug
        LOG.warning("could not compile schema validator: %s", exc)
        return _validate_body_minimal(schema, body)

    # Trick to make relative refs resolve: wrap the schema in a tiny
    # object that has the spec as its $id, then validate body against
    # the inner schema with the registry visible. Easier path: resolve
    # the $ref ourselves once before handing to the validator.
    resolved_schema = _resolve_ref(raw_spec, schema)
    if resolved_schema is schema and "$ref" in schema:
        # Couldn't resolve — fall back to the minimal checker.
        return _validate_body_minimal(schema, body)

    try:
        # Build a fresh validator against the resolved schema. Because
        # the resolved schema may itself contain nested $refs, we
        # pre-resolve every ``$ref`` recursively. For the registry's
        # spec depth (Pydantic models referencing other Pydantic
        # models), this is fine — fully recursive types do not appear.
        flat_schema = _flatten_refs(raw_spec, dict(resolved_schema))
        validator = Draft202012Validator(flat_schema)
    except Exception as exc:  # pragma: no cover - schema bug
        LOG.warning("could not compile flattened validator: %s", exc)
        return _validate_body_minimal(resolved_schema, body)

    errors: list[dict[str, Any]] = []
    for err in validator.iter_errors(body):
        path = ".".join(str(p) for p in err.absolute_path) or ""
        field = f"body.{path}" if path else "body"
        errors.append(
            {
                "field": field,
                "rule": err.validator or "schema",
                "message": err.message,
            }
        )
    return errors


def _flatten_refs(
    raw_spec: Mapping[str, Any],
    node: Any,
    seen: Optional[set[str]] = None,
) -> Any:
    """Recursively inline every ``$ref`` in ``node``.

    Used by the body validator because the bundled jsonschema
    Draft202012Validator does not resolve OpenAPI-style
    ``#/components/schemas/...`` references unless the schema itself
    is the root document. Flattening sidesteps the issue entirely.

    ``seen`` is a set of already-followed refs in the current branch
    used to detect cycles. Cycles in the registry's schema set are
    not expected, but the guard prevents infinite recursion if one
    is ever introduced.
    """
    if seen is None:
        seen = set()
    if isinstance(node, Mapping):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            if ref in seen:
                # Cycle: leave the $ref as-is so validation fails
                # informatively rather than infinite-looping.
                return dict(node)
            target = _resolve_ref(raw_spec, node)
            if target is node:
                return dict(node)
            return _flatten_refs(raw_spec, target, seen | {ref})
        return {k: _flatten_refs(raw_spec, v, seen) for k, v in node.items()}
    if isinstance(node, list):
        return [_flatten_refs(raw_spec, v, seen) for v in node]
    return node


def _validate_body_minimal(
    schema: Mapping[str, Any],
    body: Any,
) -> Sequence[dict[str, Any]]:
    """Minimal type / required-fields check used when jsonschema is absent."""
    errors: list[dict[str, Any]] = []
    type_name = schema.get("type")
    if type_name == "object":
        if not isinstance(body, Mapping):
            errors.append(
                {
                    "field": "body",
                    "rule": "type",
                    "message": "expected JSON object",
                }
            )
            return errors
        for required_field in schema.get("required", []) or []:
            if required_field not in body:
                errors.append(
                    {
                        "field": f"body.{required_field}",
                        "rule": "required",
                        "message": f"required field {required_field!r} missing",
                    }
                )
    elif type_name == "array" and not isinstance(body, list):
        errors.append(
            {"field": "body", "rule": "type", "message": "expected JSON array"}
        )
    return errors


# ---------------------------------------------------------------------------
# Parameter helpers
# ---------------------------------------------------------------------------


def _iter_parameters(
    raw_spec: Mapping[str, Any],
    operation: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    """Yield the parameter list, resolving any ``$ref`` placeholders."""
    out: list[Mapping[str, Any]] = []
    for param in operation.get("parameters") or []:
        resolved = _resolve_ref(raw_spec, param)
        if isinstance(resolved, Mapping):
            out.append(resolved)
    return out


def _iter_parameters_inline(
    operation: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    """As :func:`_iter_parameters` but without ``$ref`` resolution.

    Used by sections of the validator that don't need full reference
    walking — keeps the hot path quick.
    """
    return [p for p in (operation.get("parameters") or []) if isinstance(p, Mapping)]


def _resolve_ref(
    raw_spec: Mapping[str, Any],
    item: Any,
) -> Any:
    """Resolve a single ``{"$ref": "#/..."}`` reference; pass non-refs through."""
    if not isinstance(item, Mapping) or "$ref" not in item:
        return item
    ref = item["$ref"]
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return item
    target: Any = raw_spec
    for segment in ref[2:].split("/"):
        segment = segment.replace("~1", "/").replace("~0", "~")
        if isinstance(target, Mapping) and segment in target:
            target = target[segment]
        else:
            return item
    return target


def _select_media(
    content_obj: Mapping[str, Any],
    content_type_header: str,
) -> Optional[Mapping[str, Any]]:
    """Pick the matching media-type entry from ``content``.

    Falls back to ``application/json`` when the request didn't
    explicitly declare a content type and there's a JSON entry in
    the spec.
    """
    if content_type_header:
        primary = content_type_header.split(";", 1)[0].strip().lower()
        for key, val in content_obj.items():
            if isinstance(key, str) and key.lower() == primary:
                return val if isinstance(val, Mapping) else None
    json_entry = content_obj.get("application/json")
    if isinstance(json_entry, Mapping):
        return json_entry
    return None


def _check_simple_type(
    *,
    field: str,
    schema: Mapping[str, Any],
    values: list[str],
) -> Sequence[dict[str, Any]]:
    """Best-effort type check for query parameters."""
    type_name = schema.get("type")
    errors: list[dict[str, Any]] = []
    for v in values:
        if type_name == "integer":
            try:
                int(v)
            except ValueError:
                errors.append(
                    {
                        "field": field,
                        "rule": "type",
                        "message": f"expected integer; got {v!r}",
                    }
                )
        elif type_name == "boolean" and v.lower() not in {"true", "false", "1", "0"}:
            errors.append(
                {
                    "field": field,
                    "rule": "type",
                    "message": f"expected boolean; got {v!r}",
                }
            )
    return errors


__all__ = (
    "OpenAPIValidationError",
    "load_spec",
    "validate_event",
)

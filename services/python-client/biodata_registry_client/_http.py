"""
Single HTTP adapter shared by every method on :class:`BioDataRegistryClient`.

Centralizing the request flow here gives us four guarantees in one
place:

1. **Auth on every call.** The adapter pulls a non-expired ID token
   from the :class:`CognitoTokenSource` and sets the ``Authorization``
   header before the wire send. Per-method code never touches the
   token cache directly, so an accidentally-unauthenticated method
   would have to deliberately bypass this function.

2. **Property 14 error decoding (R30.5).** Every non-2xx response is
   parsed for ``{"code", "message", "details", "request_id", ...}``
   and surfaced as the matching typed exception via
   :func:`exception_for_code`. Unknown codes fall back to
   :class:`RegistryError` so a forward-compatible client never crashes
   on a code it doesn't yet recognize.

3. **Retry-After hoisting.** ``RateLimited`` exceptions get
   ``retry_after_s`` populated from the HTTP header (preferred) or
   the response body (fallback). Callers can write a single
   ``except RateLimited as e: time.sleep(e.retry_after_s)`` loop
   without parsing headers themselves.

4. **JSON parsing safety.** A 5xx that returns HTML (e.g. an API
   Gateway integration error) is surfaced as a structured
   :class:`RegistryError` rather than blowing up on
   ``json.loads``.

This module is internal — callers go through
:class:`BioDataRegistryClient`.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional
from urllib.parse import urlencode, urljoin

import requests

from biodata_registry_client._errors import (
    ErrorCode,
    RateLimited,
    RegistryError,
    exception_for_code,
)
from biodata_registry_client._token import CognitoTokenSource

# Default per-call timeout. Set deliberately high to accommodate
# Bedrock-backed endpoints (``/search/nl``, ``/agent/chat``) which can
# take 5–10s on cold paths. Per-call overrides are supported.
DEFAULT_TIMEOUT_S = 30.0


def send(
    *,
    base_url: str,
    method: str,
    path: str,
    token_source: Optional[CognitoTokenSource],
    json_body: Any = None,
    query: Optional[Mapping[str, Any]] = None,
    extra_headers: Optional[Mapping[str, str]] = None,
    expected_status: tuple[int, ...] = (200, 201, 202, 204),
    timeout_s: float = DEFAULT_TIMEOUT_S,
    session: Optional[requests.Session] = None,
) -> Any:
    """Send a single request, raising typed exceptions on non-2xx.

    Parameters
    ----------
    base_url:
        API root, e.g. ``https://api.biodata-registry.alleninstitute.org``.
    method:
        HTTP method, uppercase.
    path:
        Path beginning with a leading ``/``, e.g. ``/assets/{id}``.
        The caller is responsible for URL-encoding any path
        parameters before substitution.
    token_source:
        Token cache; ``None`` skips the auth header (used only by the
        public ``/healthz`` endpoint and the public path of ``/search``).
    json_body:
        If not ``None``, sent as the JSON body with
        ``Content-Type: application/json``.
    query:
        Query parameters. ``None`` values are dropped (a common
        OpenAPI generator convention).
    extra_headers:
        Optional headers merged on top of the auth + content-type
        defaults.
    expected_status:
        Tuple of statuses the caller treats as success. Anything
        outside this tuple is decoded as a Property 14 error.
    timeout_s:
        Per-request timeout (seconds). The default of 30s aligns with
        API Gateway's hard 29s integration timeout plus a small
        margin for connection overhead.
    session:
        Optional requests session (e.g. for connection pooling or
        custom adapters in tests). When ``None`` we create a one-shot
        session per call — fine for the PoC throughput; real
        deployments should pool.

    Returns
    -------
    The JSON-decoded response body, or ``None`` for 204 No Content.
    """
    headers: dict[str, str] = {"Accept": "application/json"}
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    if token_source is not None:
        headers["Authorization"] = f"Bearer {token_source.get()}"
    if extra_headers:
        headers.update(extra_headers)

    url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    if query:
        # Drop None values so optional OpenAPI params don't go on the
        # wire as "?foo=None"; the server would reject those as
        # malformed.
        cleaned = {k: v for k, v in query.items() if v is not None}
        # Render bools as lowercase JSON strings — matches the
        # OpenAPI schema and what the server's openapi-core middleware
        # accepts.
        for k, v in list(cleaned.items()):
            if isinstance(v, bool):
                cleaned[k] = "true" if v else "false"
        if cleaned:
            url = f"{url}?{urlencode(cleaned, doseq=True)}"

    body_kwargs: dict[str, Any] = {}
    if json_body is not None:
        body_kwargs["data"] = json.dumps(json_body)

    sess = session if session is not None else requests
    response = sess.request(
        method=method,
        url=url,
        headers=headers,
        timeout=timeout_s,
        **body_kwargs,
    )

    if response.status_code in expected_status:
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            # 2xx with non-JSON body is unexpected. Surface as a
            # structured error rather than silently returning bytes,
            # which would confuse typed-call sites.
            raise RegistryError(
                "Server returned a non-JSON 2xx response",
                http_status=response.status_code,
            )

    raise _decode_error(response)


def _decode_error(response: requests.Response) -> RegistryError:
    """Convert an error response into the matching typed exception.

    The decoder is forgiving: server bugs, network proxies, and
    misconfigured API Gateway integrations can all produce non-JSON
    or partially-shaped error bodies. We normalize whatever we get
    into a :class:`RegistryError`-derived instance.
    """
    status = response.status_code
    request_id = response.headers.get("x-amzn-RequestId") or response.headers.get(
        "X-Request-Id"
    )

    body: Mapping[str, Any]
    try:
        decoded = response.json()
        body = decoded if isinstance(decoded, Mapping) else {}
    except ValueError:
        body = {}

    code = body.get("code")
    message = body.get("message") or _default_message_for_status(status)
    details = body.get("details")
    # Body request_id wins over header — the server populated it
    # deliberately for log correlation, while the header is set by
    # API Gateway and may not match the Lambda's idea of the request.
    body_request_id = body.get("request_id")
    if body_request_id:
        request_id = body_request_id

    exc_cls = exception_for_code(code) if code else _exception_for_status(status)
    exc = exc_cls(
        message,
        details=details,
        request_id=request_id,
        http_status=status,
    )

    # Hoist Retry-After to the typed exception (header takes
    # precedence; body ``details.retry_after_s`` is the documented
    # fallback per design.md §Error Code Mapping).
    if isinstance(exc, RateLimited):
        retry_after = response.headers.get("Retry-After")
        retry_after_s: Optional[int] = None
        if retry_after is not None:
            try:
                retry_after_s = int(retry_after)
            except ValueError:
                # Retry-After can be an HTTP-date; for the PoC we only
                # honor the integer-seconds form (which is what API
                # Gateway emits per design.md). Leave None on parse
                # failure so callers fall through to default backoff.
                retry_after_s = None
        if retry_after_s is None and isinstance(details, Mapping):
            body_value = details.get("retry_after_s")
            if isinstance(body_value, int):
                retry_after_s = body_value
        exc.retry_after_s = retry_after_s

    return exc


def _default_message_for_status(status: int) -> str:
    return {
        400: "Bad request",
        401: "Authentication required",
        403: "Forbidden",
        404: "Not found",
        409: "Conflict",
        422: "Validation failed",
        429: "Rate limit exceeded",
    }.get(status, f"HTTP {status}")


def _exception_for_status(status: int) -> type[RegistryError]:
    """Fallback when the server didn't include a ``code`` field.

    Real API Gateway / Lambda responses always include ``code`` per
    Property 14, but pre-Lambda responses (API Gateway-generated 401s
    from a missing JWT, 403s from WAF, 429s from usage-plan throttles)
    sometimes don't. Map by status so callers still get a typed
    exception.
    """
    # 400 has multiple codes in the closed set (MISSING_PROVENANCE,
    # INVALID_HIERARCHY) — when we don't know which, fall through to
    # the base class rather than misleading the caller into catching
    # the wrong subtype. The HTTP status is still on exc.http_status.
    by_status = {
        401: ErrorCode.UNAUTHORIZED,
        403: ErrorCode.FORBIDDEN,
        404: ErrorCode.NOT_FOUND,
        409: ErrorCode.INVALID_STATE_TRANSITION,
        422: ErrorCode.VALIDATION_FAILED,
        429: ErrorCode.RATE_LIMITED,
    }
    return exception_for_code(by_status.get(status, ErrorCode.INTERNAL_ERROR))


__all__ = ("DEFAULT_TIMEOUT_S", "send")

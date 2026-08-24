# `biodata-registry-client` — Python client for the Allen BioData Registry

Pip-installable wrapper around the registry's REST API. Hand-authored
to mirror the shape that `openapi-python-client` would emit from
`openapi/openapi.yaml` (Task 13.1) so that switching to the generated
client later is a drop-in change.

## Why hand-authored?

`openapi-python-client` was not installed in the PoC build environment.
Per Task 13.2's deliverables ("prefer a hand-authored wrapper over
fighting the tool"), this package ships with the same public surface a
generated client would expose:

- One method per OpenAPI `operationId`.
- Typed exceptions matching the Property 14 `code` field (R30.5).
- Transparent Cognito token refresh in an interceptor (R15.3).
- `validated_only=True` query-level filter on `list_assets` and
  `search` (R15.5).

When the generator becomes available, regenerate with:

```bash
openapi-python-client generate \
    --path openapi/openapi.yaml \
    --output-path services/python-client/biodata_registry_client/
```

The token-refresh interceptor and typed-exception machinery in
`_token.py` / `_errors.py` / `_http.py` are independent of the
generated request methods, so they can be retained on top of the
generator's output with minimal churn (the only edit needed will be
to wire the generator's per-operation `httpx.AsyncClient` calls
through `_http.send`).

## Install

```bash
pip install biodata-registry-client
```

For development:

```bash
pip install -e ".[test]"
```

## Quickstart

```python
from biodata_registry_client import BioDataRegistryClient

client = BioDataRegistryClient(
    api_url="https://api.biodata-registry.alleninstitute.org",
    cognito_user_pool_id="us-west-2_AbcDefGhi",
    cognito_app_client_id="6qj3...",  # User Pool app client id
    region="us-west-2",
    refresh_token="eyJjdHkiOiJKV1Qi...",  # from your auth flow
)

# Create an asset (synchronous duplicate check runs inline; soft
# warnings come back on the 201 response).
asset = client.create_asset({
    "storage_uri": "s3://aind-data/raw/2026-03-24/sub-001/",
    "modalities": ["ephys"],
    "name": "Subject 001 — ephys recording session",
})
print(asset["id"], asset.get("warnings", []))

# Fetch it back.
fetched = client.get_asset(asset["id"])

# Move it through the lifecycle.
client.register_asset(asset["id"])
client.publish_asset(asset["id"])  # raises ValidationFailed if not valid

# Faceted search (works with or without auth — see the OpenAPI spec
# for which endpoints are public).
hits = client.search(q="ephys mus musculus", validated_only=True, limit=20)
for item in hits["items"]:
    print(item["id"], item["name"])
```

## Token refresh — how it works

The constructor builds a `CognitoTokenSource` that:

1. Uses your `id_token` (if you passed one) until ~5 minutes before
   its `exp` claim.
2. On expiry, calls `cognito-idp:InitiateAuth` with
   `AuthFlow=REFRESH_TOKEN_AUTH` to mint a fresh ID token.
3. Caches the new token until 5 minutes before its `exp`.
4. Is thread-safe — concurrent requests share a single in-flight
   refresh.

You don't need to do anything special. Every authenticated method
goes through the same `_http.send` adapter, which calls
`token_source.get()` once per request. The 5-minute skew is
configurable:

```python
from biodata_registry_client import BioDataRegistryClient, CognitoTokenSource

source = CognitoTokenSource(
    cognito_user_pool_id="us-west-2_AbcDefGhi",
    cognito_app_client_id="6qj3...",
    region="us-west-2",
    refresh_token="eyJjdHkiOiJKV1Qi...",
    refresh_skew_seconds=600,  # refresh 10 minutes before exp
)
client = BioDataRegistryClient(api_url="https://...", token_source=source)
```

You can also supply a pre-minted `id_token` and skip the boto3 path
entirely (useful in test fixtures or short-lived scripts):

```python
client = BioDataRegistryClient(
    api_url="https://...",
    id_token="eyJraWQi...",  # your already-minted token
    cognito_user_pool_id="",  # unused when no refresh occurs
    cognito_app_client_id="",
    region="",
)
```

## Error handling — typed exceptions per error code

Every non-2xx response is decoded into a typed exception drawn from
`biodata_registry_client._errors`. The exception class matches the
`code` field in the Property 14 envelope:

| Server `code`              | Exception class               | HTTP |
| -------------------------- | ----------------------------- | ---- |
| `VALIDATION_FAILED`        | `ValidationFailed`            | 422  |
| `INVALID_STATE_TRANSITION` | `InvalidStateTransition`      | 409  |
| `INVALID_HIERARCHY`        | `InvalidHierarchy`            | 400  |
| `MISSING_PROVENANCE`       | `MissingProvenance`           | 400  |
| `DUPLICATE_ENTITY`         | `DuplicateEntity`             | 409  |
| `FORBIDDEN`                | `Forbidden`                   | 403  |
| `SENSITIVE_ACCESS_DENIED`  | `SensitiveAccessDenied`       | 403  |
| `RATE_LIMITED`             | `RateLimited`                 | 429  |
| `UNAUTHORIZED`             | `Unauthorized`                | 401  |
| `NOT_FOUND`                | `NotFound`                    | 404  |

All inherit from `RegistryError`. Catch the base when you want
generic error handling:

```python
from biodata_registry_client import (
    BioDataRegistryClient,
    RateLimited,
    SensitiveAccessDenied,
    ValidationFailed,
)

try:
    asset = client.get_asset(asset_id)
except SensitiveAccessDenied:
    # Caller has structural visibility but lacks the sensitive-flag
    # privilege. Show a "request elevated access" UI.
    ...
except ValidationFailed as e:
    # e.details is the per-field error list from Property 14.
    for field_err in e.details or []:
        print(field_err["field"], field_err["rule"])
except RateLimited as e:
    # Retry-After is parsed from the header (preferred) or the body.
    if e.retry_after_s is not None:
        time.sleep(e.retry_after_s)
```

## Extending

- **Custom HTTP session** — pass `session=requests.Session()` to the
  constructor. Useful for adding retries with `urllib3.util.retry`,
  custom adapters, or Allen-internal proxy handling.
- **Custom timeout** — `request_timeout_s` on the constructor. The
  default of 30s aligns with API Gateway's hard 29s integration
  timeout. Increase only for the Bedrock-backed `nl_search` and
  `agent_chat` endpoints if you need longer generations.
- **Token-source sharing** — pass `token_source=` to share a token
  cache across multiple clients (e.g. a service that calls both
  `BioDataRegistryClient` and another Cognito-fronted service).

## Running the tests

```bash
cd services/python-client
pip install -e ".[test]"
pytest
```

The test suite uses the [`responses`](https://github.com/getsentry/responses)
library to mock HTTP at the `requests.Session` adapter level, and an
injectable clock + Cognito-client double for the token-refresh logic.
No live AWS calls.

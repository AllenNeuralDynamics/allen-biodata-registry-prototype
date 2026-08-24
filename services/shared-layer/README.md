# Shared Lambda Layer — `biodata_registry_shared`

Source tree for the Lambda Layer attached to every business Lambda in
the Allen BioData Registry PoC. The Layer ships:

- **Runtime dependencies** (`requirements.txt`):
  - `aind-data-schema>=2.7,<3.0` — Pydantic v2 models for AIND
    biological metadata (R1, R2, R33).
  - `psycopg[binary]>=3.2,<4.0` — Aurora connection driver. We pick
    psycopg over pg8000 (which the bring-up Lambdas use) because
    business Lambdas need real connection-pool / async support and
    modern parameter binding.
  - `openapi-core>=0.19,<0.24` — OpenAPI 3.0 request validation
    middleware (R14.5). The package itself reaches for openapi-core
    only on the body-validation path; the rest of the middleware uses
    the bundled `jsonschema` directly.
  - `boto3` — **NOT bundled.** The AWS Lambda Python 3.12 runtime
    ships a recent boto3 already. Bundling a second copy in the Layer
    would double the cold-start unzip cost and risk version drift.

- **Internal helpers** (the `biodata_registry_shared/` Python package):
  - `auth_context.py` — parses the API Gateway authorizer event into
    a typed, frozen `AuthContext` dataclass (R19.4, R19.5). Validates
    UUIDs, normalizes role tokens, rejects unknown roles, supports
    both REST API v1 and HTTP API v2 event shapes.
  - `db.py` — **`aurora_connection(auth, ...)` context manager**.
    Mints an Aurora IAM DB auth token (or reads from Secrets Manager
    when `secret_arn=` is passed), opens a TLS-enabled psycopg
    connection, opens a transaction, issues the four
    `SET LOCAL app.current_*` GUCs that drive Postgres RLS (Layer 2 —
    R10.1, R10.2), and commits-or-rolls-back-and-closes on exit.
  - `errors.py` — typed exception classes per error code +
    `make_error_response` shaper producing the standardized
    Property 14 payload (R30). Includes
    `error_response_from_exception` adapter that yields the API
    Gateway proxy response shape.
  - `role_helpers.py` — `require_role(auth, "org_admin")`,
    `require_space_access(auth, space_id)`, `is_data_admin(auth)`,
    `is_org_admin(auth, org_id)`, `is_privileged_for_sensitive(auth)`
    (Layer 1 — R10.4, R8.5).
  - `sensitive_flag.py` — `check_sensitive_flag(asset, auth)` raises
    `SensitiveAccessDenied` for non-privileged callers when the asset
    is flagged (Layer 3 — R8.1, R8.2).
  - `openapi_middleware.py` — `load_spec(...)` + `validate_event(loaded, event)`
    validates a request against the hand-authored `openapi.yaml`
    (R14.5). Inlines `$ref`s recursively before invoking
    `jsonschema.Draft202012Validator` to sidestep
    OpenAPI-style-`#/components/schemas/...` resolution issues.
  - `logging_config.py` — `configure_logging()` + `bind_request_id(rid)`
    structured-JSON logger for CloudWatch Logs Insights uniformity.

**Validates:** R8.1, R8.2, R10.1, R10.2, R10.4, R14.5, R19.4, R19.5,
R30 (all sub-clauses), R33.1, R33.2.

**Design references:**
- `design.md` §Components.Lambda Functions (shared Layer).
- `design.md` §Architecture.RLS Enforcement Architecture.
- `design.md` §Error Handling.Error Code Mapping.

---

## Repository layout

```
services/shared-layer/
├── biodata_registry_shared/
│   ├── __init__.py            # Public re-exports
│   ├── auth_context.py
│   ├── db.py
│   ├── errors.py
│   ├── logging_config.py
│   ├── openapi_middleware.py
│   ├── role_helpers.py
│   └── sensitive_flag.py
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── test_auth_context.py     # unit + property-based
│   ├── test_db.py
│   ├── test_errors.py           # unit + property-based (Property 14)
│   ├── test_logging_config.py
│   ├── test_openapi_middleware.py
│   ├── test_role_helpers.py
│   └── test_sensitive_flag.py
├── pyproject.toml
├── requirements.txt             # ← runtime source of truth for the Layer
└── README.md
```

The Terraform module
(`terraform/modules/lambda-layer/`) packages
`biodata_registry_shared/` plus the runtime deps from
`requirements.txt` into a Lambda Layer zip. `pyproject.toml` is
test/dev tooling only and plays no role in the live image.

---

## Local development

```bash
cd customers/NPO/RSC/Allen_Institute/biodata-registry/services/shared-layer

# Install runtime + test deps in editable mode:
python3 -m pip install -e .[test]

# Run unit + property-based tests:
python3 -m pytest -q
```

Tests are deliberately self-contained: they mock psycopg, boto3, and
the file system, so no AWS account or running Postgres is required.
The property-based tests run with Hypothesis at 200 examples per
property by default — bump via the standard
`HYPOTHESIS_PROFILE=ci` environment variable for a longer CI run.

The repository root's `.hypothesis/` directory caches generated
examples between runs.

---

## Public API surface (re-exported from `biodata_registry_shared`)

```python
from biodata_registry_shared import (
    # Auth context (R19)
    AuthContext, parse_auth_context, AuthContextError,

    # DB connection (R10.1, R10.2)
    AuroraConnectionConfig, aurora_connection,

    # Errors (R30 / Property 14)
    ErrorCode, RegistryError,
    ValidationFailed, InvalidStateTransition, InvalidHierarchy,
    MissingProvenance, DuplicateEntity, Forbidden,
    SensitiveAccessDenied, RateLimited, Unauthorized, NotFound,
    make_error_response, error_response_from_exception, exception_for_code,

    # RLS guards (R8.5, R10.4)
    require_role, require_space_access,
    is_data_admin, is_org_admin,
    DATA_ADMIN_ROLES, PRIVILEGED_SENSITIVE_ROLES,
    check_sensitive_flag,

    # OpenAPI middleware (R14.5)
    load_spec, validate_event, OpenAPIValidationError,

    # Structured logging
    configure_logging, bind_request_id, get_logger,
)
```

A typical business Lambda handler looks like:

```python
from biodata_registry_shared import (
    aurora_connection, bind_request_id, configure_logging,
    error_response_from_exception, parse_auth_context,
    RegistryError, require_role,
)

configure_logging()  # idempotent; safe at module import

def handler(event, context):
    request_id = (
        event.get("requestContext", {}).get("requestId")
        or context.aws_request_id
    )
    with bind_request_id(request_id):
        try:
            auth = parse_auth_context(event)
            require_role(auth, ["org_admin", "admin"])

            with aurora_connection(auth) as conn:
                # All queries here are RLS-filtered automatically.
                with conn.cursor() as cur:
                    cur.execute("SELECT id, name FROM data_asset LIMIT 10")
                    rows = cur.fetchall()

            return {
                "statusCode": 200,
                "body": json.dumps([{"id": r[0], "name": r[1]} for r in rows]),
            }
        except RegistryError as exc:
            return error_response_from_exception(exc, request_id=request_id)
```

---

## Authorizer context contract

The Authorizer Lambda (Task 15.1) emits the following fields on the
API Gateway authorizer context. `parse_auth_context` is the single
canonical place those fields are decoded.

| Field | Type on wire | Required | Notes |
|---|---|---|---|
| `user_id` | string (UUID) | yes | The registry's `app_user.id`. |
| `cognito_sub` | string (UUID) | yes | JWT `sub`. The `sub` alias is also accepted. |
| `email` | string | yes | Verified at Cognito sign-up. |
| `roles` | comma-separated string OR list | yes | Members must be in `{admin, org_admin, space_admin, data_administrator, viewer}`. |
| `org_ids` | comma-separated string OR list | yes (may be empty) | UUIDs the user holds an org-level role on. |
| `space_ids` | comma-separated string OR list | yes (may be empty) | UUIDs the user has access to (direct + inherited + via sharing grants). |

When called on a full Lambda-proxy event, `parse_auth_context`
auto-extracts the dict from `event["requestContext"]["authorizer"]`
(REST API v1) or `event["requestContext"]["authorizer"]["jwt"]["claims"]`
(HTTP API v2). Top-level authorizer fields override the JWT claims
when both are present — that's the registry's convention.

---

## Postgres RLS GUC contract

`aurora_connection` issues four `SET LOCAL` statements on every
connection check-out, before any business SQL runs. The GUC names
exactly match those expected by `migrations/0006_rls_policies.sql`:

| GUC | Wire format | Source |
|---|---|---|
| `app.current_user_id` | UUID string | `auth.user_id` |
| `app.current_org_ids` | comma-joined UUIDs | `auth.org_ids` (sorted for determinism) |
| `app.current_space_ids` | comma-joined UUIDs | `auth.space_ids` (sorted) |
| `app.current_user_role_set` | comma-joined role tokens | `auth.roles` (sorted) |

Empty sets are encoded as the empty string (`""`), which the
migration's `string_to_array(coalesce(current_setting(...), ''), ',')`
decoder handles correctly.

`SET LOCAL` clears at COMMIT/ROLLBACK, so the GUCs cannot leak
between transactions. The connection itself is closed on context-
manager exit; we do not pool.

---

## Property-based testing

The Layer ships two of the registry's correctness-property tests as
unit-level Tier 1 PBT (per `design.md` §Testing Strategy):

- **Property 14 (Error Response Shape Correctness)** — `tests/test_errors.py`
  generates random `(code, message, details, request_id)` tuples and
  asserts every error response carries the five required fields and
  is JSON-serializable.
- **Auth context round-trip** — `tests/test_auth_context.py` generates
  well-formed authorizer payloads and asserts that
  `parse(payload).to_guc_payload()` preserves the user id, normalizes
  the role set, and produces strings (no None) — so the migration's
  `coalesce(current_setting(...), '')` decode works.

Tier 2 / Tier 3 PBT for the broader properties (Property 1, 2, 3, 6,
etc.) live closer to the code they validate (per the spec's task
plan); the Layer's own tests deliberately cover only the helpers in
this package.

---

## Limitations and upgrade paths

- **Build-platform sensitivity:** the Layer's binary wheels
  (`psycopg-binary`, `pydantic-core`) must match the Lambda runtime
  (`python3.12` on Amazon Linux 2023, x86_64). Operators on macOS or
  Windows need to build inside a Linux container —
  `public.ecr.aws/lambda/python:3.12` is the canonical image. The
  current Terraform module documents this caveat but does not yet
  containerize the build.
- **Unidirectional layer publishing:** new versions are always
  published; old versions are never deleted automatically. Operators
  must manually GC unused versions (see the Terraform module's
  README for the runbook).
- **Single-runtime targeting:** the module's
  `compatible_runtimes = ["python3.12"]` default reflects the
  registry's choice. Multi-runtime Layers complicate version tracking
  and are deferred to production.

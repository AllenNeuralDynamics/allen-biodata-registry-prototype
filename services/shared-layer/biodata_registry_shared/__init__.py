"""
Allen BioData Registry PoC — shared Lambda Layer package.

Every business Lambda in the registry attaches this Layer, which gives
it a single import surface for:

* :mod:`biodata_registry_shared.auth_context` — parsing the API Gateway
  authorizer event into a typed :class:`AuthContext` (R19.4, R19.5).
* :mod:`biodata_registry_shared.db` — the RLS-aware Aurora connection
  helper that mints an IAM DB auth token, opens a TLS-enabled psycopg
  connection, and issues the four ``SET LOCAL app.current_*`` GUCs that
  drive Postgres row-level security (R10.1, R10.2, three-layer RLS
  Layer 2).
* :mod:`biodata_registry_shared.errors` — typed exception classes and
  the ``make_error_response`` shaper that produces the standardized
  ``{code, message, details, request_id, timestamp}`` payload required
  by Property 14 (R30).
* :mod:`biodata_registry_shared.role_helpers` — application-level
  guards (``require_role``, ``require_space_access``, ``is_data_admin``)
  that implement Layer 1 of the three-layer RLS model.
* :mod:`biodata_registry_shared.sensitive_flag` — the Layer 3
  ``check_sensitive_flag`` helper enforcing R8 sensitive-data access
  control on direct GETs and search-result hydration.
* :mod:`biodata_registry_shared.openapi_middleware` — request body /
  parameter validation against the hand-authored ``openapi.yaml``
  (R14.5).
* :mod:`biodata_registry_shared.logging_config` — structured JSON
  logging with request_id propagation.

Design references:
* design.md §Components.Lambda Functions (shared Layer).
* design.md §Architecture.RLS Enforcement Architecture.
* design.md §Error Handling.Error Code Mapping.
"""

from __future__ import annotations

__version__ = "0.1.0"

# Re-export the public entry points so callers can write
# ``from biodata_registry_shared import AuthContext, ValidationFailed,
# aurora_connection, make_error_response`` without knowing which module
# each lives in. This keeps caller code stable if we later refactor the
# internal module layout.
from biodata_registry_shared.auth_context import (
    AuthContext,
    AuthContextError,
    parse_auth_context,
)
from biodata_registry_shared.db import (
    AuroraConnectionConfig,
    aurora_connection,
)
from biodata_registry_shared.errors import (
    Conflict,
    DuplicateEntity,
    ErrorCode,
    Forbidden,
    InvalidHierarchy,
    InvalidStateTransition,
    MissingProvenance,
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
from biodata_registry_shared.logging_config import (
    bind_request_id,
    configure_logging,
    get_logger,
)
from biodata_registry_shared.openapi_middleware import (
    OpenAPIValidationError,
    load_spec,
    validate_event,
)
from biodata_registry_shared.role_helpers import (
    DATA_ADMIN_ROLES,
    PRIVILEGED_SENSITIVE_ROLES,
    is_data_admin,
    is_org_admin,
    require_role,
    require_space_access,
)
from biodata_registry_shared.sensitive_flag import check_sensitive_flag

__all__ = (
    "AuthContext",
    "AuthContextError",
    "AuroraConnectionConfig",
    "Conflict",
    "DATA_ADMIN_ROLES",
    "DuplicateEntity",
    "ErrorCode",
    "Forbidden",
    "InvalidHierarchy",
    "InvalidStateTransition",
    "MissingProvenance",
    "NotFound",
    "OpenAPIValidationError",
    "PRIVILEGED_SENSITIVE_ROLES",
    "RateLimited",
    "RegistryError",
    "SensitiveAccessDenied",
    "Unauthorized",
    "ValidationFailed",
    "__version__",
    "aurora_connection",
    "bind_request_id",
    "check_sensitive_flag",
    "configure_logging",
    "error_response_from_exception",
    "exception_for_code",
    "get_logger",
    "is_data_admin",
    "is_org_admin",
    "load_spec",
    "make_error_response",
    "parse_auth_context",
    "require_role",
    "require_space_access",
    "validate_event",
)

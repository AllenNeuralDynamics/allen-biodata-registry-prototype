"""
Allen BioData Registry PoC — Python client library.

Pip-installable wrapper over the registry's REST API. Imitates the
shape that ``openapi-python-client`` would produce from
``openapi/openapi.yaml`` so that:

* The PoC build environment does not need the generator installed
  (the generator is the ideal source — see this package's README for
  the regeneration command — but a hand-authored equivalent keeps
  the PoC moving when the tool is unavailable).
* Switching to the generated client later is a drop-in change: the
  public surface of :class:`BioDataRegistryClient` and the typed
  exceptions in this module match what
  ``openapi-python-client --output-path biodata_registry_client/``
  would emit, modulo Pydantic model dataclasses (which the registry
  passes around as plain ``dict[str, Any]`` in this PoC).

Top-level surface
-----------------

The package exposes three things:

1. :class:`BioDataRegistryClient` — the request-shaped facade. One
   method per OpenAPI ``operationId``. Every call goes through the
   shared :func:`~biodata_registry_client._http.send` adapter which:

   - mints / refreshes a Cognito ID token via the
     :class:`~biodata_registry_client._token.CognitoTokenSource`,
   - sets ``Authorization: Bearer <id_token>``,
   - parses the Property 14 error envelope on non-2xx responses and
     raises the matching typed exception per
     :func:`~biodata_registry_client._errors.exception_for_code`.

2. The typed exception hierarchy rooted at
   :class:`RegistryError`. These are intentionally local copies of
   the classes shipped in the shared Lambda Layer
   (``services/shared-layer/biodata_registry_shared/errors.py``) so
   that ``pip install biodata-registry-client`` does not pull in
   ``aind-data-schema`` and ``psycopg`` — Layer-only dependencies
   external consumers should not have to install. The wire codes are
   identical, so a server-emitted ``VALIDATION_FAILED`` raises an
   instance of *this* package's :class:`ValidationFailed`.

3. The :class:`~biodata_registry_client._token.CognitoTokenSource`
   helper. Most callers will not instantiate this directly — the
   client constructs one internally — but it is exported for
   advanced use cases (e.g. a long-lived service that wants to share
   a single token cache across multiple clients).

Validates: R15.1, R15.2, R15.3, R15.4, R30.5; design.md §External
Interfaces.Python Client.
"""

from biodata_registry_client._errors import (
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
    exception_for_code,
)
from biodata_registry_client._token import CognitoTokenSource
from biodata_registry_client.client import BioDataRegistryClient

__all__ = (
    "BioDataRegistryClient",
    "CognitoTokenSource",
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
    "exception_for_code",
)

__version__ = "0.1.0"

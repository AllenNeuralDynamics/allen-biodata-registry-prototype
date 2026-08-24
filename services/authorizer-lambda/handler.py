"""
Allen BioData Registry PoC — API Gateway Lambda Authorizer.

This Lambda is the API Gateway custom authorizer (REQUEST type) that
sits in front of every authenticated endpoint. It is invoked once per
incoming request (subject to the 5-minute API Gateway authorizer cache
configured by Terraform) and is responsible for two things:

1. **Validate the Cognito JWT.** Verify the signature, expiration,
   audience, issuer, and ``token_use`` claim. The JWKS is fetched from
   Cognito's ``.well-known/jwks.json`` endpoint and cached at module
   level for one hour to amortize cold-start cost.

2. **Resolve the auth context from Aurora.** Look up
   ``app_user.id`` from the JWT's ``sub`` claim, then aggregate roles
   from ``user_org_role`` and ``user_space_role`` and discover any
   active ``sharing_grant`` rows where the user is the principal. The
   resulting ``{user_id, org_ids, space_ids, roles}`` set is returned
   in the API Gateway authorizer policy's ``context`` field — every
   downstream business Lambda parses this via
   :func:`biodata_registry_shared.parse_auth_context` and uses the
   tuple to seed Postgres RLS GUCs.

On any failure (malformed header, invalid JWT, missing ``app_user``
row), the function raises ``Unauthorized`` so API Gateway returns
401. Authorization-level decisions (which endpoints a role can
invoke) are made downstream — this Lambda only decides "is this
caller a known, authenticated user?".

Validates: R9.7, R14.4, R19.4, R19.5.
Design references:
  * design.md §Components.1. Authorizer_Lambda.
  * design.md §Architecture.RLS Enforcement Architecture (Layer 0 —
    auth context resolution that seeds RLS Layer 2).

Caching contract
----------------

API Gateway's built-in REQUEST authorizer cache is configured to
``authorizer_result_ttl_in_seconds = 300`` (Task 14.1, 15.1). This
means a successful Allow policy is reused for up to 5 minutes for the
same Authorization header value — Aurora is **not** hit on every
request. When a role/sharing-grant changes, the cache is busted by
Governance_Lambda invalidating Redis ``Access_Filter_Cache`` (Task
26.1) and by waiting out the 5-minute TTL — eventual-consistency
boundaries are documented in design.md §Architecture.Cache Coherence.

Operational contract
--------------------

* The function reads Aurora connection parameters from env vars
  injected by Terraform (``DB_HOST``, ``DB_PORT``, ``DB_NAME``,
  ``DB_USER``). Authentication uses **IAM database authentication**
  via ``boto3.rds.generate_db_auth_token`` — no static passwords.
* JWT validation uses ``PyJWT`` with the ``cryptography`` backend so
  ``RS256`` signatures (the algorithm Cognito issues) verify with the
  RSA public keys served from JWKS.
* The function logs structured one-line JSON-ish events with the
  Cognito sub prefix only — never the full token, never the email,
  per the spec's PII handling guidance.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Mapping, Optional, Tuple

import boto3
import jwt  # PyJWT
import psycopg
from jwt import PyJWKClient

LOG = logging.getLogger()
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


# JWKS cache TTL in seconds. JWKS keys rotate roughly daily on Cognito,
# so a 1-hour cache strikes a balance between cold-start cost (re-fetch
# is a single HTTPS GET) and freshness — a rotated key still propagates
# within an hour.
_JWKS_CACHE_TTL_S = 3600

# Module-level cache: (jwks_url) -> (PyJWKClient, expires_at_epoch).
# A lock guards concurrent refreshes so two warm invocations sharing a
# container don't issue duplicate JWKS fetches.
_jwks_cache: Dict[str, Tuple[PyJWKClient, float]] = {}
_jwks_cache_lock = threading.Lock()


class Unauthorized(Exception):
    """Raised whenever the request must be denied at the auth boundary.

    API Gateway interprets the literal exception message ``"Unauthorized"``
    as an instruction to return HTTP 401 to the client (vs the 500 it
    would otherwise return for any other raised exception). We follow
    that contract by ensuring ``str(exc) == "Unauthorized"`` whenever
    the exception bubbles up to AWS — internal helpers raise this with
    a richer message and the top-level handler scrubs it.
    """


def handler(event: Mapping[str, Any], context: Any) -> Mapping[str, Any]:
    """API Gateway REQUEST authorizer entry point.

    Parameters
    ----------
    event:
        The API Gateway Lambda authorizer event. Relevant fields:
          * ``event["headers"]["Authorization"]`` — Bearer JWT.
          * ``event["methodArn"]`` — ARN of the invoked method, used as
            the policy's ``Resource``.
    context:
        Standard Lambda context object — unused.

    Returns
    -------
    The IAM policy document API Gateway expects.

    Raises
    ------
    Unauthorized
        On any auth failure. API Gateway converts this into HTTP 401.
    """
    request_id = getattr(context, "aws_request_id", None) or "unknown"
    LOG.info("authorizer invoked", extra={"request_id": request_id})

    try:
        token = _extract_bearer_token(event)
        method_arn = _extract_method_arn(event)

        # 1. Validate the Cognito JWT.
        claims = _verify_cognito_jwt(token)
        cognito_sub = claims["sub"]
        email = claims.get("email") or ""

        # 2. Resolve the registry's auth context from Aurora.
        auth_context = _resolve_auth_context(cognito_sub=cognito_sub)

        # 3. Build the Allow policy with the context fields downstream
        #    Lambdas need.
        return _build_allow_policy(
            principal_id=cognito_sub,
            # Issue an Allow for every method in this API stage, not just
            # the method we were called for. Without this the
            # authorizer_result_ttl_in_seconds cache returns the stale
            # path-specific Allow even when the user calls a different
            # path within the cache window. Computed by truncating
            # method_arn after "/<stage>/" and appending "*/*".
            method_arn=_wildcard_method_arn(method_arn),
            user_id=auth_context["user_id"],
            cognito_sub=cognito_sub,
            email=email or auth_context["email"],
            roles=auth_context["roles"],
            org_ids=auth_context["org_ids"],
            space_ids=auth_context["space_ids"],
        )
    except Unauthorized as exc:
        # Log the rich reason internally; surface only the literal
        # "Unauthorized" string to API Gateway so the client sees 401.
        LOG.warning(
            "authorizer denied request",
            extra={"request_id": request_id, "reason": str(exc)},
        )
        raise Unauthorized("Unauthorized") from exc
    except Exception as exc:  # pragma: no cover - defensive
        # Anything unexpected (DB connection failure, etc.) is also 401
        # at the boundary — failing open would let unauthenticated
        # requests through. We log loudly so the operational dashboard
        # picks the spike up.
        LOG.exception(
            "authorizer failed unexpectedly",
            extra={"request_id": request_id, "error_type": type(exc).__name__},
        )
        raise Unauthorized("Unauthorized") from exc


# ---------------------------------------------------------------------------
# JWT extraction + validation
# ---------------------------------------------------------------------------


def _extract_bearer_token(event: Mapping[str, Any]) -> str:
    """Pull the JWT out of the Authorization header.

    API Gateway delivers headers in two shapes depending on the
    authorizer trigger configuration:

    * For REQUEST-type authorizers with the new identity-source format,
      the headers live at ``event["headers"]``.
    * Some legacy / TOKEN-type configurations put the token directly
      at ``event["authorizationToken"]``.

    We accept either shape because callers in tests + the API Gateway
    wiring may use either.
    """
    if not isinstance(event, Mapping):
        raise Unauthorized(
            f"event is not a mapping; got {type(event).__name__}"
        )

    # Preferred path: REQUEST authorizer with headers.
    headers = event.get("headers") or {}
    if isinstance(headers, Mapping):
        # Header names are case-insensitive per RFC 7230; API Gateway
        # preserves the casing the client sent. Accept both.
        for key in ("Authorization", "authorization"):
            raw = headers.get(key)
            if isinstance(raw, str) and raw.strip():
                return _strip_bearer_prefix(raw.strip())

    # Fallback: TOKEN authorizer.
    token = event.get("authorizationToken")
    if isinstance(token, str) and token.strip():
        return _strip_bearer_prefix(token.strip())

    raise Unauthorized("missing Authorization header / authorizationToken")


def _strip_bearer_prefix(value: str) -> str:
    """Strip an optional ``Bearer `` prefix (case-insensitive)."""
    lowered = value.lower()
    if lowered.startswith("bearer "):
        return value[7:].strip()
    return value


def _extract_method_arn(event: Mapping[str, Any]) -> str:
    """Pull the invoked method ARN.

    API Gateway always populates this for REQUEST authorizers; missing
    it indicates a malformed event (or a unit-test calling the handler
    without setting it up). We default to a wildcard ARN if absent so
    the policy still has a syntactically valid ``Resource`` — but we
    log a warning since this is not expected in production.
    """
    method_arn = event.get("methodArn")
    if isinstance(method_arn, str) and method_arn:
        return method_arn
    LOG.warning("authorizer event missing methodArn; using wildcard")
    return "*"


def _wildcard_method_arn(method_arn: str) -> str:
    """Convert a specific method_arn to a wildcard for the same stage.

    Input:  arn:aws:execute-api:us-west-2:014097726564:abc123/dev/POST/assets
    Output: arn:aws:execute-api:us-west-2:014097726564:abc123/dev/*/*

    Lets API Gateway cache one Allow that covers every endpoint in the
    stage, instead of a Allow that becomes a 403 the moment the caller
    hits a different path within the cache TTL.
    """
    parts = method_arn.split("/")
    if len(parts) < 4:
        return method_arn
    return "/".join(parts[:2]) + "/*/*"


def _verify_cognito_jwt(token: str) -> Mapping[str, Any]:
    """Validate the JWT against Cognito's JWKS.

    Performs all four of: signature verification, expiration check,
    audience match (against ``COGNITO_APP_CLIENT_ID``), issuer match
    (against the constructed Cognito issuer URL), and ``token_use``
    check.

    The JWKS is fetched from Cognito and cached at module scope for an
    hour. A one-hour staleness window is acceptable: Cognito rotates
    signing keys roughly daily, and any token signed with a rotated-out
    key is rejected by the JWKS lookup until the next refresh.

    Raises ``Unauthorized`` on any validation failure.
    """
    user_pool_id = _required_env("COGNITO_USER_POOL_ID")
    app_client_id = _required_env("COGNITO_APP_CLIENT_ID")
    region = _resolve_region()

    issuer = f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"
    jwks_url = f"{issuer}/.well-known/jwks.json"

    jwk_client = _get_jwks_client(jwks_url)

    try:
        signing_key = jwk_client.get_signing_key_from_jwt(token)
    except jwt.exceptions.PyJWKClientError as exc:
        raise Unauthorized(f"failed to find JWKS signing key: {exc}") from exc
    except jwt.exceptions.DecodeError as exc:
        raise Unauthorized(f"malformed JWT: {exc}") from exc

    # Cognito ID tokens validate the audience claim ('aud'); access
    # tokens use 'client_id'. We accept ID tokens here per design.md
    # §Components.1: the Web App is expected to send the ID token, and
    # it carries the user's email which the registry needs for audit.
    try:
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=app_client_id,
            issuer=issuer,
            options={
                "require": ["exp", "iat", "iss", "aud", "sub", "token_use"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_iat": True,
                "verify_iss": True,
                "verify_aud": True,
            },
        )
    except jwt.exceptions.ExpiredSignatureError as exc:
        raise Unauthorized("JWT expired") from exc
    except jwt.exceptions.InvalidAudienceError as exc:
        raise Unauthorized(f"JWT audience mismatch: {exc}") from exc
    except jwt.exceptions.InvalidIssuerError as exc:
        raise Unauthorized(f"JWT issuer mismatch: {exc}") from exc
    except jwt.exceptions.InvalidSignatureError as exc:
        raise Unauthorized(f"JWT signature invalid: {exc}") from exc
    except jwt.exceptions.MissingRequiredClaimError as exc:
        raise Unauthorized(f"JWT missing required claim: {exc}") from exc
    except jwt.exceptions.InvalidTokenError as exc:
        raise Unauthorized(f"JWT invalid: {exc}") from exc

    token_use = claims.get("token_use")
    if token_use != "id":
        # design.md §Components.1 specifies the ID token. Access tokens
        # would carry only the sub + scopes, no email — we'd lose the
        # ability to write to audit logs.
        raise Unauthorized(
            f"token_use must be 'id' (got {token_use!r}); access tokens are not accepted"
        )

    if not claims.get("sub"):
        raise Unauthorized("JWT missing 'sub' claim")

    return claims


def _get_jwks_client(jwks_url: str) -> PyJWKClient:
    """Return a cached :class:`PyJWKClient`, refreshing every hour.

    PyJWKClient performs the actual JWKS fetch lazily on its first
    ``get_signing_key_from_jwt`` call and caches keys internally. We
    discard the entire client every TTL so a key rotation that drops
    a previously-active kid is reflected in the cache within an hour.
    """
    now = time.time()
    with _jwks_cache_lock:
        cached = _jwks_cache.get(jwks_url)
        if cached is not None and cached[1] > now:
            return cached[0]

        # Build a fresh client. PyJWKClient handles its own urllib
        # fetch; we just supply the URL.
        client = PyJWKClient(jwks_url, cache_keys=True, lifespan=_JWKS_CACHE_TTL_S)
        _jwks_cache[jwks_url] = (client, now + _JWKS_CACHE_TTL_S)
        return client


# ---------------------------------------------------------------------------
# Aurora lookup
# ---------------------------------------------------------------------------


def _resolve_auth_context(*, cognito_sub: str) -> Mapping[str, Any]:
    """Look up the user's identity + roles + spaces from Aurora.

    Performs a single connection's worth of work:

    1. Resolve ``app_user.id`` (and email) from ``cognito_sub``.
    2. Aggregate role tokens across ``user_org_role`` + ``user_space_role``.
    3. Compute ``org_ids`` (every org with a direct org-level role).
    4. Compute ``space_ids`` from three sources:
        * Direct grants in ``user_space_role``.
        * Inherited via org-level roles: every space in any org where
          the user has an org-level role (org admins / data
          administrators / viewers see all their org's spaces).
        * Active ``sharing_grant`` rows where the user is the
          principal (``principal_user_id``) or the user's home org is
          the principal (``principal_org_id``) or the user's space is
          the grantee (``grantee_space_id``) or the user's org is the
          grantee (``grantee_org_id``). Grants whose ``expires_at`` has
          passed are excluded.

    Raises
    ------
    Unauthorized
        If no ``app_user`` row exists for the JWT's ``sub`` (sign-in
        flow should have created the row via Post-Confirmation; if it
        didn't, that's a bug worth surfacing).
    """
    db_host = _required_env("DB_HOST")
    db_port = int(os.environ.get("DB_PORT", "5432"))
    db_name = _required_env("DB_NAME")
    db_user = _required_env("DB_USER")
    region = _resolve_region()

    token = _generate_iam_auth_token(
        host=db_host, port=db_port, user=db_user, region=region
    )

    conn = _connect_aurora(
        host=db_host,
        port=db_port,
        database=db_name,
        user=db_user,
        password=token,
    )
    try:
        with conn.cursor() as cur:
            user_row = _fetch_app_user(cur, cognito_sub)
            if user_row is None:
                raise Unauthorized(
                    f"no app_user row for cognito_sub prefix {cognito_sub[:8]!r}; "
                    "Post-Confirmation Lambda may not have run"
                )
            user_id, email = user_row

            org_roles = _fetch_org_roles(cur, user_id)
            space_roles = _fetch_space_roles(cur, user_id)
            org_ids_for_inheritance = {org_id for (org_id, _role) in org_roles}
            inherited_spaces = (
                _fetch_spaces_for_orgs(cur, list(org_ids_for_inheritance))
                if org_ids_for_inheritance
                else set()
            )
            shared_spaces = _fetch_sharing_spaces(
                cur,
                user_id=user_id,
                org_ids=list(org_ids_for_inheritance),
                space_ids=[space_id for (space_id, _role) in space_roles],
            )

        # Aggregate. All UUIDs are stringified at the boundary so the
        # API Gateway context (string-only) round-trips cleanly.
        roles = sorted({role for (_o, role) in org_roles} | {role for (_s, role) in space_roles})
        org_ids = sorted({str(o) for (o, _r) in org_roles})
        space_ids = sorted(
            {str(s) for (s, _r) in space_roles}
            | {str(s) for s in inherited_spaces}
            | {str(s) for s in shared_spaces}
        )

        return {
            "user_id": str(user_id),
            "email": email,
            "roles": roles,
            "org_ids": org_ids,
            "space_ids": space_ids,
        }
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover - defensive
            LOG.exception("error closing Aurora connection (non-fatal)")


def _fetch_app_user(cur: Any, cognito_sub: str) -> Optional[Tuple[str, str]]:
    """Return ``(app_user.id, email)`` for the given Cognito ``sub``."""
    cur.execute(
        "SELECT id, email FROM app_user WHERE cognito_sub = %s",
        (cognito_sub,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return (str(row[0]), str(row[1]))


def _fetch_org_roles(cur: Any, user_id: str) -> List[Tuple[str, str]]:
    """Return ``[(org_id, role)]`` for every ``user_org_role`` row.

    A user can hold multiple roles on the same org (e.g. ``viewer`` +
    ``data_administrator``); we return every (org, role) tuple and let
    the caller deduplicate as needed.
    """
    cur.execute(
        "SELECT org_id, role::text FROM user_org_role WHERE user_id = %s",
        (user_id,),
    )
    return [(str(o), str(r)) for (o, r) in cur.fetchall()]


def _fetch_space_roles(cur: Any, user_id: str) -> List[Tuple[str, str]]:
    """Return ``[(space_id, role)]`` for every ``user_space_role`` row."""
    cur.execute(
        "SELECT space_id, role::text FROM user_space_role WHERE user_id = %s",
        (user_id,),
    )
    return [(str(s), str(r)) for (s, r) in cur.fetchall()]


def _fetch_spaces_for_orgs(cur: Any, org_ids: List[str]) -> set[str]:
    """Return every ``space.id`` belonging to any of the given orgs.

    Org-level roles inherit access to every space inside the org —
    this mirrors the RLS policy in ``0006_rls_policies.sql`` which
    treats ``current_org_ids`` as a parent set.
    """
    if not org_ids:
        return set()
    cur.execute(
        "SELECT id FROM space WHERE org_id = ANY(%s::uuid[])",
        (org_ids,),
    )
    return {str(s) for (s,) in cur.fetchall()}


def _fetch_sharing_spaces(
    cur: Any,
    *,
    user_id: str,
    org_ids: List[str],
    space_ids: List[str],
) -> set[str]:
    """Return ``space.id`` set for every active ``sharing_grant`` the user has access to.

    A user is a sharing-grant principal when any of:

    * ``principal_user_id`` matches the user.
    * ``principal_org_id`` matches an org the user belongs to (via
      org-level role).
    * ``grantee_space_id`` matches a space the user has a direct space
      role on.
    * ``grantee_org_id`` matches an org the user has a role on.

    The grant must not be expired (``expires_at IS NULL OR expires_at > now()``).

    For each matching grant, the granter_org's spaces become visible
    — that's the rule the RLS policies enforce in
    ``0006_rls_policies.sql``. We materialize the set here so the
    Authorizer's ``space_ids`` includes them, which (a) feeds the RLS
    GUC and (b) feeds OpenSearch's filter clause for non-privileged
    search.
    """
    # We pass empty arrays as ``ARRAY[]::uuid[]`` so the ``= ANY`` is
    # well-typed — Postgres complains about a bare ``= ANY('{}')``
    # without an explicit cast.
    sql = """
        SELECT DISTINCT s.id
        FROM sharing_grant sg
        JOIN space s ON s.org_id = sg.granter_org_id
        WHERE (sg.expires_at IS NULL OR sg.expires_at > now())
          AND (
                sg.principal_user_id = %s
             OR sg.principal_org_id  = ANY(%s::uuid[])
             OR sg.grantee_space_id  = ANY(%s::uuid[])
             OR sg.grantee_org_id    = ANY(%s::uuid[])
          )
    """
    cur.execute(sql, (user_id, org_ids, space_ids, org_ids))
    return {str(s) for (s,) in cur.fetchall()}


# ---------------------------------------------------------------------------
# IAM policy construction
# ---------------------------------------------------------------------------


def _build_allow_policy(
    *,
    principal_id: str,
    method_arn: str,
    user_id: str,
    cognito_sub: str,
    email: str,
    roles: List[str],
    org_ids: List[str],
    space_ids: List[str],
) -> Mapping[str, Any]:
    """Return the API Gateway authorizer-policy document.

    The ``context`` field is restricted to scalar strings — API Gateway
    flattens nested types and silently drops ``null`` values. We
    therefore comma-join the three list-valued fields so they
    round-trip cleanly to
    :func:`biodata_registry_shared.parse_auth_context`, which knows to
    split them.
    """
    return {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": "Allow",
                    "Resource": method_arn,
                }
            ],
        },
        "context": {
            "user_id": user_id,
            "cognito_sub": cognito_sub,
            "email": email,
            "roles": ",".join(roles),
            "org_ids": ",".join(org_ids),
            "space_ids": ",".join(space_ids),
        },
    }


# ---------------------------------------------------------------------------
# DB connection helpers
# ---------------------------------------------------------------------------


def _generate_iam_auth_token(*, host: str, port: int, user: str, region: str) -> str:
    """Mint an Aurora IAM database authentication token.

    Tokens are valid for 15 minutes; we generate a fresh token per
    invocation rather than caching, because the token mint is local
    and cheap (<10ms — it's a signed string, no network round trip).
    """
    rds = boto3.client("rds", region_name=region)
    return rds.generate_db_auth_token(
        DBHostname=host,
        Port=port,
        DBUsername=user,
        Region=region,
    )


def _connect_aurora(
    *,
    host: str,
    port: int,
    database: str,
    user: str,
    password: str,
) -> Any:
    """Open a TLS-enabled psycopg connection to Aurora.

    Aurora's parameter group rejects unencrypted connections; we
    enforce SSL via ``sslmode=require``. The Authorizer is read-only,
    so autocommit is safe and saves a round-trip vs. opening an
    explicit transaction.
    """
    return psycopg.connect(
        host=host,
        port=port,
        dbname=database,
        user=user,
        password=password,
        sslmode=os.environ.get("DB_SSLMODE", "require"),
        connect_timeout=int(os.environ.get("DB_CONNECT_TIMEOUT_SECONDS", "5")),
        autocommit=True,
    )


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise Unauthorized(
            f"required environment variable {name!r} is not set; "
            "Terraform should inject this for the Authorizer Lambda"
        )
    return value


def _resolve_region() -> str:
    """Resolve the AWS region from the Lambda runtime env."""
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-west-2"
    )

"""
Allen BioData Registry PoC — Cognito Post-Confirmation Lambda.

Cognito invokes this function once a user finishes confirming their account
(verification code on self-signup, or SAML completion when federation is
enabled later). The function inserts a bare ``app_user`` row into Aurora
PostgreSQL with the user's Cognito ``sub`` and email, and **no role
assignments** — the user can authenticate immediately but sees only
``lifecycle_state = 'published'`` data via Aurora RLS until an org admin
grants them a role through the Governance Lambda's access-request flow.

Validates: R19.3 (Post-Confirmation creates an ``app_user`` row).
Design references:
  * design.md §Components.User Onboarding Flow (sequence diagram).
  * design.md §Components.Lambda Functions (the cognito module owns this
    Lambda; it is counted separately from the 13 business Lambdas).

Operational contract:

* The function reads Aurora connection parameters from env vars injected
  by Terraform (``DB_HOST``, ``DB_PORT``, ``DB_NAME``, ``DB_USER``).
* Authentication uses **IAM database authentication** — boto3
  ``rds.generate_db_auth_token`` produces a short-lived (15-minute) token
  used as the Postgres password. There are **no static passwords** in
  config or env.
* The INSERT statement is ``INSERT … ON CONFLICT (cognito_sub) DO
  NOTHING`` so a Cognito retry (e.g. transient DB error after a
  successful insert) does not raise a unique-violation error and does not
  duplicate rows. Idempotency is therefore enforced by the database, not
  by application logic.
* On any unexpected failure (network, DB error, missing required event
  fields), the function raises — Cognito sees the failure, returns a
  non-fatal error to the user-facing flow, and retries up to its built-in
  retry budget. **The function deliberately does not swallow errors**:
  silently dropping ``app_user`` rows would leave authenticated users
  with no Aurora identity, breaking every downstream RLS-aware query.
* On success, the original event is returned unchanged so Cognito
  completes the post-confirmation step normally.

Optional ``org_id`` handling
----------------------------

The ``custom:org_id`` Cognito attribute is informational — the SAML
attribute mapping (or self-signup flow) may surface a pending
Organization affiliation. Whether this Lambda persists that value to an
``app_user.org_id`` column depends on whether migration 7.1 has added
the column. To stay deployable today (current schema has no ``org_id``)
**and** ready for the column being added later, the env var
``APP_USER_HAS_ORG_ID`` gates inclusion of the column in the INSERT
statement. Default is ``false``. Once migration 7.1 lands, the dev
composition flips this to ``true`` without code change.
"""

from __future__ import annotations

import logging
import os
import ssl
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

import boto3

# pg8000 is pure-Python (no libpq native dependency), so the Lambda
# deployment package needs no platform-specific wheel — `terraform
# apply` from any developer workstation produces a working zip. We use
# the DB-API 2.0 surface for portability.
import pg8000.dbapi  # type: ignore[import-untyped]

LOG = logging.getLogger()
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


# Stable name of the Aurora extension/default schema. The bare ``app_user``
# table lives in ``public`` per design.md §Data Models.Aurora.
_TABLE_NAME = "app_user"


class PostConfirmationError(RuntimeError):
    """Raised when the event cannot be processed.

    Cognito treats any exception raised by the Post-Confirmation trigger
    as a failure. The framework retries according to its built-in
    schedule, so raising — rather than returning a sentinel — is the
    correct way to signal a transient or permanent failure back to
    Cognito's invocation pipeline.
    """


def handler(event: Mapping[str, Any], context: Any) -> Mapping[str, Any]:
    """Cognito Post-Confirmation entry point.

    Parameters
    ----------
    event:
        The Cognito Post-Confirmation event. Relevant fields:
          * ``event["userName"]`` — the user pool username.
          * ``event["request"]["userAttributes"]["sub"]`` — the Cognito
            UUID. **This is the value persisted as ``app_user.cognito_sub``**
            (NOT ``userName``, which can collide across pools).
          * ``event["request"]["userAttributes"]["email"]`` — required.
          * ``event["request"]["userAttributes"]["custom:org_id"]`` —
            optional pending Organization affiliation.
    context:
        Standard Lambda context object — unused.

    Returns
    -------
    The original event, unchanged. Cognito requires the trigger to
    return the event so the auth flow can continue.
    """
    LOG.info("post-confirmation invoked", extra={"trigger_source": event.get("triggerSource")})

    cognito_sub, email, org_id = _extract_user_attributes(event)
    insert_org_id = _bool_env("APP_USER_HAS_ORG_ID", default=False)

    db_host = _required_env("DB_HOST")
    db_port = int(os.environ.get("DB_PORT", "5432"))
    db_name = _required_env("DB_NAME")
    db_user = _required_env("DB_USER")
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"

    token = _generate_iam_auth_token(host=db_host, port=db_port, user=db_user, region=region)

    conn = _connect_aurora(
        host=db_host,
        port=db_port,
        database=db_name,
        user=db_user,
        password=token,
    )
    try:
        _insert_app_user(
            conn=conn,
            cognito_sub=cognito_sub,
            email=email,
            org_id=org_id if insert_org_id else None,
            include_org_id_column=insert_org_id,
        )
    finally:
        # Connection close failures are not fatal — the row is already
        # committed by the time we get here, and Lambda will reap the
        # worker on cold-start anyway.
        try:
            conn.close()
        except Exception:  # pragma: no cover - defensive
            LOG.exception("error closing Aurora connection (non-fatal)")

    LOG.info(
        "post-confirmation succeeded",
        extra={"cognito_sub_prefix": cognito_sub[:8], "has_org_id": bool(org_id)},
    )

    # Cognito requires the original event back so the framework can
    # continue. Mutating it is unnecessary for this trigger.
    return event


# ---------------------------------------------------------------------------
# Helpers — small, individually testable pieces of the pipeline.
# ---------------------------------------------------------------------------


def _extract_user_attributes(event: Mapping[str, Any]) -> Tuple[str, str, Optional[str]]:
    """Pull (cognito_sub, email, org_id) out of a Cognito event.

    Raises
    ------
    PostConfirmationError
        If either the Cognito sub or the email attribute is missing.
        Both are NOT NULL UNIQUE in the ``app_user`` schema, so we fail
        loudly rather than letting Postgres surface a less helpful
        constraint-violation error later in the pipeline.
    """
    request = event.get("request") or {}
    user_attrs = request.get("userAttributes") or {}

    # The Cognito UUID lives at userAttributes.sub. ``event.userName`` may
    # equal the sub in some configurations, but it is not guaranteed —
    # using the explicit attribute is the only stable identifier.
    cognito_sub = user_attrs.get("sub")
    email = user_attrs.get("email")
    org_id = user_attrs.get("custom:org_id") or None

    if not cognito_sub:
        raise PostConfirmationError(
            "Cognito Post-Confirmation event missing required 'sub' attribute under request.userAttributes"
        )
    if not email:
        raise PostConfirmationError(
            "Cognito Post-Confirmation event missing required 'email' attribute under request.userAttributes"
        )

    # Strip leading/trailing whitespace from email defensively. The
    # Cognito hosted UI validates this server-side, but a custom
    # AdminCreateUser caller could submit padded values.
    email = email.strip()

    return cognito_sub, email, org_id


def _generate_iam_auth_token(*, host: str, port: int, user: str, region: str) -> str:
    """Generate an Aurora IAM database authentication token.

    Tokens are valid for 15 minutes. We generate a fresh token on every
    invocation rather than caching, because Lambda containers can be
    long-lived (min 5 min, up to several hours) — a cached token would
    expire mid-warm-pool. Generation is cheap (<10ms; signed locally,
    no API call).
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
    """Open an SSL-enabled pg8000 connection to Aurora.

    SSL is required: Aurora's parameter group rejects unencrypted
    connections to the writer endpoint. We use the system trust store
    (``ssl.create_default_context()``) — Aurora certificates chain to
    Amazon's root CAs, which are present on every Lambda execution
    environment.
    """
    ssl_ctx = ssl.create_default_context()
    return pg8000.dbapi.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        ssl_context=ssl_ctx,
        timeout=int(os.environ.get("DB_CONNECT_TIMEOUT_SECONDS", "10")),
    )


def _insert_app_user(
    *,
    conn: Any,
    cognito_sub: str,
    email: str,
    org_id: Optional[str],
    include_org_id_column: bool,
) -> None:
    """Idempotent insert of a bare ``app_user`` row.

    The ``ON CONFLICT (cognito_sub) DO NOTHING`` clause makes this safe
    against:

    * Cognito retries (the same trigger event delivered twice).
    * Race conditions where two concurrent confirmations for the same
      sub arrive (shouldn't happen, but the constraint guarantees we
      end up with exactly one row regardless).

    We commit explicitly because pg8000 is autocommit-off by default.
    """
    columns: Sequence[str] = ("cognito_sub", "email")
    placeholders: Sequence[str] = ("%s", "%s")
    params: list[Any] = [cognito_sub, email]

    if include_org_id_column:
        columns = (*columns, "org_id")
        placeholders = (*placeholders, "%s")
        params.append(org_id)

    sql = (
        f"INSERT INTO {_TABLE_NAME} ({', '.join(columns)}) "
        f"VALUES ({', '.join(placeholders)}) "
        f"ON CONFLICT (cognito_sub) DO NOTHING"
    )

    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
    conn.commit()


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise PostConfirmationError(
            f"Required environment variable {name!r} is not set; "
            "Terraform should inject Aurora connection params via env vars."
        )
    return value


def _bool_env(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}

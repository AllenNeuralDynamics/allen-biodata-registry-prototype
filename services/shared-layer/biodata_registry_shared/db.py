"""
Allen BioData Registry PoC — Aurora connection helper (RLS Layer 2).

This module exposes the **single, blessed** way for business Lambdas
to open a Postgres connection. Direct ``psycopg.connect`` is banned by
lint rule (Task 29.1.2) — every path through Aurora must go through
:func:`aurora_connection` so the RLS GUCs (``app.current_user_id``,
``app.current_org_ids``, ``app.current_space_ids``,
``app.current_user_role_set``) are guaranteed to be set before the
first SQL statement runs.

What the helper does, in order:

1. Resolve credentials. Two modes:

   * **IAM database authentication (default).** Mints a fresh
     15-minute token via boto3 ``rds.generate_db_auth_token``. No
     static password ever touches the Lambda.
   * **Secrets Manager (opt-in).** If ``secret_arn`` is provided,
     fetches the JSON-shaped secret and reads ``password``. Needed
     only by paths that authenticate as the Aurora master user
     (out-of-band bootstrap), which are not the steady-state.

2. Open a TLS-enabled :class:`psycopg.Connection`. Aurora's parameter
   group rejects unencrypted connections, so SSL is mandatory.

3. Begin a transaction explicitly (``BEGIN``). Even though psycopg's
   default ``autocommit = False`` wraps every statement in a transaction,
   we issue ``BEGIN`` explicitly so the ``SET LOCAL`` GUCs below are
   bound to a known transaction lifetime.

4. Issue four ``SET LOCAL app.current_*`` statements seeded from the
   passed-in :class:`AuthContext`. ``SET LOCAL`` clears at COMMIT /
   ROLLBACK — the matching teardown is implicit in step 6.

5. Yield the connection to the caller. The caller runs business
   queries; each one is RLS-filtered by the GUCs from step 4 (see
   migration ``0006_rls_policies.sql`` — every policy reads
   ``current_setting('app.current_*', true)``).

6. On clean exit: commit. On exception: rollback. Either way: close
   the connection.

Validates: R10.1, R10.2 (Layer 2); R19.4, R19.5; design.md
§Architecture.RLS Enforcement Architecture (Layer 2).
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import os
from typing import Any, Iterator, Mapping, Optional, Protocol, runtime_checkable

# psycopg is shipped by the Layer's requirements.txt at runtime. For
# unit tests, we still want to import-check this module without
# psycopg installed — so the import is conditional and the helper
# raises a clear error if invoked without it.
try:
    import psycopg  # type: ignore[import-untyped]
    from psycopg import sql  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - exercised only when psycopg missing
    psycopg = None  # type: ignore[assignment]
    sql = None  # type: ignore[assignment]

from biodata_registry_shared.auth_context import AuthContext

LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Connection config
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class AuroraConnectionConfig:
    """Where to connect.

    Reads sensible defaults from environment variables (the Lambda
    Terraform module sets these for every business Lambda), but
    every field is overridable for tests and for one-off scripts.

    Attributes
    ----------
    host:
        Aurora writer endpoint. Falls back to ``DB_HOST`` env var.
    port:
        Aurora port. Falls back to ``DB_PORT``, then ``5432``.
    database:
        Database name. Falls back to ``DB_NAME``.
    user:
        DB user the Lambda authenticates as. Falls back to ``DB_USER``.
    region:
        AWS region for IAM token generation. Falls back to
        ``AWS_REGION`` / ``AWS_DEFAULT_REGION`` / ``us-west-2``.
    sslmode:
        psycopg SSL mode. ``require`` is the minimum for Aurora; the
        default ``verify-full`` plus a CA bundle is the production
        choice but requires the AmazonRootCA bundle to be present —
        we default to ``require`` for the PoC and document the
        upgrade path in the module README.
    connect_timeout_s:
        TCP / TLS handshake timeout (seconds). Falls back to
        ``DB_CONNECT_TIMEOUT_SECONDS`` / ``10``.
    statement_timeout_ms:
        Per-statement timeout enforced via ``SET LOCAL statement_timeout``.
        Pass ``0`` to disable. Falls back to ``DB_STATEMENT_TIMEOUT_MS``
        / ``10000`` (10 s).
    """

    host: str
    database: str
    user: str
    port: int = 5432
    region: str = "us-west-2"
    sslmode: str = "require"
    connect_timeout_s: int = 10
    statement_timeout_ms: int = 10_000

    @classmethod
    def from_env(cls, **overrides: Any) -> "AuroraConnectionConfig":
        """Build a config from the standard env-var contract."""
        env_values = {
            "host": os.environ.get("DB_HOST"),
            "port": int(os.environ.get("DB_PORT", "5432")),
            "database": os.environ.get("DB_NAME"),
            "user": os.environ.get("DB_USER"),
            "region": (
                os.environ.get("AWS_REGION")
                or os.environ.get("AWS_DEFAULT_REGION")
                or "us-west-2"
            ),
            "sslmode": os.environ.get("DB_SSLMODE", "require"),
            "connect_timeout_s": int(
                os.environ.get("DB_CONNECT_TIMEOUT_SECONDS", "10")
            ),
            "statement_timeout_ms": int(
                os.environ.get("DB_STATEMENT_TIMEOUT_MS", "10000")
            ),
        }
        env_values.update(overrides)

        # The error message names the env var (DB_HOST, etc.) rather than
        # the field — operators set the env var, not the dataclass field.
        env_var_for_field = {
            "host": "DB_HOST",
            "database": "DB_NAME",
            "user": "DB_USER",
        }
        for field_name, env_var in env_var_for_field.items():
            if not env_values.get(field_name):
                raise RuntimeError(
                    f"AuroraConnectionConfig.from_env: required env var "
                    f"{env_var!r} is missing or empty"
                )
        return cls(**env_values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pluggable hooks (override-able for tests)
# ---------------------------------------------------------------------------


@runtime_checkable
class _ConnectionFactory(Protocol):
    """Anything that returns a psycopg-compatible connection.

    The signature matches :func:`psycopg.connect` so swapping in a
    test double is mechanical. Callers usually leave this at the
    default (real psycopg); unit tests override it to inject an
    in-memory fake connection that records the executed SQL.
    """

    def __call__(self, **kwargs: Any) -> Any: ...  # pragma: no cover


@runtime_checkable
class _AuthTokenProvider(Protocol):
    """Function that mints an Aurora IAM auth token.

    The default implementation calls boto3's ``rds.generate_db_auth_token``.
    Tests inject a stub returning a known string so we can assert the
    token is what gets passed to the connection factory.
    """

    def __call__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        region: str,
    ) -> str: ...  # pragma: no cover


def _default_auth_token_provider(
    *,
    host: str,
    port: int,
    user: str,
    region: str,
) -> str:
    """Mint an Aurora IAM DB auth token via boto3.

    Imports boto3 lazily so unit tests that override the provider
    don't need boto3 installed.
    """
    import boto3  # type: ignore[import-untyped]

    rds = boto3.client("rds", region_name=region)
    return rds.generate_db_auth_token(
        DBHostname=host,
        Port=port,
        DBUsername=user,
        Region=region,
    )


def _default_secrets_manager_password(secret_arn: str, region: str) -> str:
    """Read a Secrets Manager secret and extract ``password``."""
    import boto3  # type: ignore[import-untyped]

    sm = boto3.client("secretsmanager", region_name=region)
    response = sm.get_secret_value(SecretId=secret_arn)
    raw = response.get("SecretString")
    if not raw:
        raise RuntimeError(
            f"Secrets Manager secret {secret_arn!r} has no SecretString"
        )
    payload = json.loads(raw)
    if not isinstance(payload, Mapping) or "password" not in payload:
        raise RuntimeError(
            f"Secrets Manager secret {secret_arn!r} JSON does not contain "
            "a 'password' field"
        )
    return str(payload["password"])


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def aurora_connection(
    auth: AuthContext,
    *,
    config: Optional[AuroraConnectionConfig] = None,
    secret_arn: Optional[str] = None,
    db_user: Optional[str] = None,
    connection_factory: Optional[_ConnectionFactory] = None,
    auth_token_provider: Optional[_AuthTokenProvider] = None,
    secret_password_provider: Optional[Any] = None,
) -> Iterator[Any]:
    """Open an RLS-aware Aurora connection.

    Parameters
    ----------
    auth:
        The caller's :class:`AuthContext`. The four ``app.current_*``
        GUCs are seeded from this value before the connection is
        yielded.
    config:
        Optional :class:`AuroraConnectionConfig`. Defaults to
        :meth:`AuroraConnectionConfig.from_env`.
    secret_arn:
        When set, authenticate using the password from the Secrets
        Manager secret rather than minting an IAM token. Used by
        out-of-band paths (master-user bootstrap, etc.); the steady-
        state Lambdas leave this ``None``.
    db_user:
        Override ``config.user`` for this connection. Useful when a
        single Lambda needs to authenticate as multiple users
        (rare — the seeder Lambda does it during bring-up).
    connection_factory / auth_token_provider / secret_password_provider:
        Test seams. Production code leaves these ``None``.

    Yields
    ------
    A live :class:`psycopg.Connection` with RLS GUCs already set and
    a transaction already open. The caller MUST NOT call ``commit``
    or ``rollback`` on it directly — the context manager does that
    based on whether the body raised.
    """
    if psycopg is None and connection_factory is None:
        raise RuntimeError(
            "psycopg is not installed and no connection_factory was provided. "
            "Install the shared Lambda Layer or pass a test factory."
        )
    if not isinstance(auth, AuthContext):
        raise TypeError(
            f"auth must be an AuthContext; got {type(auth).__name__}"
        )

    cfg = config or AuroraConnectionConfig.from_env()
    user = db_user or cfg.user

    # 1. Resolve credentials.
    password = _resolve_password(
        secret_arn=secret_arn,
        cfg=cfg,
        user=user,
        auth_token_provider=auth_token_provider or _default_auth_token_provider,
        secret_password_provider=secret_password_provider
        or _default_secrets_manager_password,
    )

    # 2. Open the TLS connection. We pass autocommit=False explicitly
    # so the BEGIN we issue below opens a real transaction rather
    # than executing as a no-op autocommit block.
    factory: _ConnectionFactory = connection_factory or psycopg.connect  # type: ignore[assignment]
    conn = factory(
        host=cfg.host,
        port=cfg.port,
        dbname=cfg.database,
        user=user,
        password=password,
        sslmode=cfg.sslmode,
        connect_timeout=cfg.connect_timeout_s,
        autocommit=False,
    )

    try:
        # 3. Open a transaction. psycopg's default mode would do this
        # implicitly on the first statement, but issuing it explicitly
        # documents the boundary for the SET LOCAL GUCs.
        _execute(conn, "BEGIN")

        # 4. Set the four RLS GUCs.
        _apply_auth_context(conn, auth)

        # 5. Per-transaction safety net. statement_timeout caps any
        # individual query so a runaway plan can't hold the
        # connection open and block compaction.
        if cfg.statement_timeout_ms > 0:
            _execute(
                conn,
                f"SET LOCAL statement_timeout = {int(cfg.statement_timeout_ms)}",
            )

        yield conn
    except Exception:
        # 6a. Roll back on any exception, then re-raise.
        try:
            conn.rollback()
        except Exception:  # pragma: no cover - defensive
            LOG.exception("rollback failed during error path (non-fatal)")
        raise
    else:
        # 6b. Commit on clean exit. The matching commit also clears
        # the SET LOCAL GUCs because of how Postgres scopes them.
        conn.commit()
    finally:
        # 6c. Always close the connection. We do not pool — the
        # Lambda execution environment is short-lived and pooling
        # introduces correctness risks (the GUCs would have to be
        # reset on every checkout).
        try:
            conn.close()
        except Exception:  # pragma: no cover - defensive
            LOG.exception("close failed (non-fatal)")


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _resolve_password(
    *,
    secret_arn: Optional[str],
    cfg: AuroraConnectionConfig,
    user: str,
    auth_token_provider: _AuthTokenProvider,
    secret_password_provider: Any,
) -> str:
    """Pick the correct credential source based on inputs."""
    if secret_arn:
        LOG.debug(
            "resolving Aurora password via Secrets Manager",
            extra={"secret_arn": secret_arn, "user": user},
        )
        return secret_password_provider(secret_arn, cfg.region)

    LOG.debug(
        "minting Aurora IAM DB auth token",
        extra={"host": cfg.host, "user": user, "region": cfg.region},
    )
    return auth_token_provider(
        host=cfg.host,
        port=cfg.port,
        user=user,
        region=cfg.region,
    )


def _apply_auth_context(conn: Any, auth: AuthContext) -> None:
    """Issue the four ``SET LOCAL app.current_*`` statements.

    We use ``set_config(name, value, is_local=true)`` rather than
    literal-string ``SET LOCAL`` so the value is bound as a parameter
    — this neutralizes any SQL-injection risk from a malformed GUC
    payload, even though :class:`AuthContext` already validates its
    inputs as UUIDs and enum tokens.

    The four GUC names are pinned to match
    ``migrations/0006_rls_policies.sql``:

    * ``app.current_user_id``
    * ``app.current_org_ids``
    * ``app.current_space_ids``
    * ``app.current_user_role_set``

    All four values are comma-separated text (empty string when the
    underlying set is empty), per the migration's
    ``string_to_array(coalesce(current_setting('app.current_*',
    true), ''), ',')`` decoding.
    """
    payload = auth.to_guc_payload()

    # Mapping is intentionally explicit (not a loop over payload
    # keys) so the GUC names appear verbatim in source — easier to
    # grep and to keep in sync with the migration.
    statements: list[tuple[str, str]] = [
        ("app.current_user_id", payload["current_user_id"]),
        ("app.current_org_ids", payload["current_org_ids"]),
        ("app.current_space_ids", payload["current_space_ids"]),
        ("app.current_user_role_set", payload["current_user_role_set"]),
    ]

    with conn.cursor() as cur:
        for guc_name, value in statements:
            # set_config(guc_name, value, is_local) returns the value
            # as text; we discard the result.
            cur.execute(
                "SELECT set_config(%s, %s, true)",
                (guc_name, value),
            )


def _execute(conn: Any, statement: str) -> None:
    """Run a parameter-less SQL statement on a fresh cursor."""
    with conn.cursor() as cur:
        cur.execute(statement)


__all__ = (
    "AuroraConnectionConfig",
    "aurora_connection",
)

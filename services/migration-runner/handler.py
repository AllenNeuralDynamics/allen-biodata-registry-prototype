"""
Allen BioData Registry PoC — Schema Migration Runner Lambda entry point.

This Lambda is invoked by Terraform (via the ``aws_lambda_invocation``
data source — see ``terraform/modules/lambdas/migration-runner``) once
the Aurora cluster + bootstrap (``vector`` extension, ``biodata_cdc``
slot) are in place. It applies every ``*.sql`` file under
``/var/task/migrations/`` in lexical order, records each applied
version in a ``schema_version`` table, and returns a structured
summary so the Terraform invocation either succeeds (no migrations
needed or all applied cleanly) or fails the apply (a SQL error).

Why a Lambda runner instead of a local-exec script
--------------------------------------------------

Aurora is in private subnets — there is no path from a developer
laptop or CI runner to the writer endpoint without operator-managed
VPN/SSM tunneling. The post-confirmation Lambda has already
established the IAM-DB-auth + VPC-attached + pg8000 pattern for this
project; reusing it for the migration runner keeps the bring-up
contained to ``terraform apply`` with no operator-side prerequisites.

Operational contract
--------------------

* Connection params come from env vars injected by Terraform:
  ``DB_HOST``, ``DB_PORT``, ``DB_NAME``, ``DB_USER``. Auth is IAM
  database authentication (``rds.generate_db_auth_token``); there are
  no static passwords.
* The ``DB_USER`` is a **privileged** Postgres role (typically
  ``migration_runner``) that can:

    - ``CREATE EXTENSION`` for ``citext``, ``pgcrypto``, ``vector``,
      ``pg_trgm`` (these are required by 0001/0002).
    - ``CREATE TABLE`` / ``CREATE TYPE`` / ``CREATE INDEX`` /
      ``ALTER TABLE`` on the public schema.
    - ``GRANT`` privileges to the per-Lambda DB users (the RLS migration
      issues GRANTs).
    - ``CREATE POLICY`` / ``ALTER TABLE … ENABLE ROW LEVEL SECURITY``
      (the 0006 migration uses these).

  In practice this means the user must be granted ``rds_superuser``
  membership (Aurora's superuser-equivalent — it is **not** the same as
  upstream Postgres ``SUPERUSER``, but it is the highest privilege
  Aurora exposes). Aurora's bootstrap (Task 3.1) creates the role; this
  Lambda only consumes it.

* Migrations directory is shipped *inside* the deployment zip at
  ``/var/task/migrations/``. The Terraform module copies the
  ``migrations/`` corpus into the build directory before
  ``archive_file`` zips it. This keeps the Lambda hermetic — no S3
  download, no runtime fetch, no surprise migrations from another
  branch.

* On any unexpected failure the Lambda raises. Terraform's
  ``aws_lambda_invocation`` data source treats the raise as a failed
  apply, which is the correct behavior for migration runs.

Validates: R32.5 (idempotent ``terraform apply``).
Design references:
  * design.md §IaC.Idempotency and Sample Data.
  * migrations/README.md (runner contract + filename convention).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import ssl
from typing import Any, Mapping

import boto3
import pg8000.dbapi  # type: ignore[import-untyped]

from runner import RunSummary, run_migrations

LOG = logging.getLogger()
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


# Default packaging location: ``terraform/modules/lambdas/migration-runner``
# copies the migrations/ corpus into the build dir under this exact
# directory so the Lambda finds them at runtime without needing extra
# config. The default can be overridden via env var for unusual layouts
# (e.g. when running the handler from a local test harness).
_DEFAULT_MIGRATIONS_DIR = "/var/task/migrations"


class MigrationRunnerLambdaError(RuntimeError):
    """Raised for handler-level failures (missing env vars, etc.).

    Errors raised by ``runner.run_migrations`` (filename violations,
    out-of-order discovery, SQL execution) propagate as their own
    exception types — see ``runner.py``.
    """


def handler(event: Mapping[str, Any], context: Any) -> Mapping[str, Any]:
    """Lambda entry point.

    Parameters
    ----------
    event:
        Optional invocation payload. The handler accepts:

        * ``{"applied_by": "<identifier>"}`` — overrides the value
          recorded into ``schema_version.applied_by``. Defaults to the
          ``DB_USER`` env var.
        * ``{"migrations_dir": "<absolute-path>"}`` — overrides where
          the runner reads ``.sql`` files from. Useful for local
          ``python handler.py`` development; production invocations
          rely on the bundled ``/var/task/migrations`` default.

        Terraform's ``aws_lambda_invocation`` typically passes ``{}``.

    context:
        Standard Lambda context. Unused.

    Returns
    -------
    A JSON-serializable summary:

    .. code-block:: python

        {
          "applied":                ["0001_governance.sql", ...],
          "skipped":                ["0002_data_asset.sql", ...],
          "drift":                  [{"version": "0003", "filename": ..., ...}],
          "schema_version_created": True,
          "elapsed_ms":             1234,
        }
    """
    LOG.info("migration-runner invoked", extra={"event_keys": sorted(event.keys() if event else [])})

    db_host = _required_env("DB_HOST")
    db_port = int(os.environ.get("DB_PORT", "5432"))
    db_name = _required_env("DB_NAME")
    db_user = _required_env("DB_USER")
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"

    migrations_dir = (
        (event or {}).get("migrations_dir")
        or os.environ.get("MIGRATIONS_DIR")
        or _DEFAULT_MIGRATIONS_DIR
    )
    applied_by = (event or {}).get("applied_by") or db_user

    token = _generate_iam_auth_token(host=db_host, port=db_port, user=db_user, region=region)

    conn = _connect_aurora(
        host=db_host,
        port=db_port,
        database=db_name,
        user=db_user,
        password=token,
    )
    try:
        summary = run_migrations(
            conn=conn,
            migrations_dir=migrations_dir,
            applied_by=applied_by,
        )
    finally:
        # Connection close failures are not fatal — anything we
        # committed is already durable.
        try:
            conn.close()
        except Exception:  # pragma: no cover - defensive
            LOG.exception("error closing Aurora connection (non-fatal)")

    LOG.info(
        "migration-runner finished",
        extra={
            "applied_count": len(summary.applied),
            "skipped_count": len(summary.skipped),
            "drift_count": len(summary.drift),
            "elapsed_ms": summary.elapsed_ms,
        },
    )

    # CloudWatch will capture the full structured summary in addition to
    # the per-event metrics above. This makes "what did this apply do?"
    # a single log search away.
    LOG.info("summary=%s", json.dumps(summary.to_dict(), default=str))

    return summary.to_dict()


# ---------------------------------------------------------------------------
# Helpers (mirrored from post-confirmation Lambda for stylistic consistency).
# ---------------------------------------------------------------------------


def _generate_iam_auth_token(*, host: str, port: int, user: str, region: str) -> str:
    """Generate an Aurora IAM database authentication token (15-min TTL).

    Generated fresh on every invocation rather than cached — Lambda
    containers can be long-lived (minutes to hours), and a cached token
    would expire mid-warm-pool. Generation is local + free (signed
    locally, no API call).
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

    Aurora's parameter group rejects unencrypted connections to the
    writer endpoint, so SSL is mandatory. The system trust store
    (``ssl.create_default_context()``) is used because Aurora
    certificates chain to Amazon's root CAs, which are present on every
    Lambda execution environment.
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


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise MigrationRunnerLambdaError(
            f"Required environment variable {name!r} is not set; "
            "Terraform should inject Aurora connection params via env vars."
        )
    return value


# Convenience for `python -m handler` style local diagnostics. Not used
# by Lambda itself.
def _summary_to_json(summary: RunSummary) -> str:  # pragma: no cover - diag aid
    return json.dumps(dataclasses.asdict(summary), default=str, indent=2)

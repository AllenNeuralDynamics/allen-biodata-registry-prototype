"""
Allen BioData Registry PoC — Sample-Data Seeder Lambda entry point.

This Lambda is invoked by Terraform (via the ``aws_lambda_invocation``
data source — see ``terraform/modules/lambdas/seeder``) once the
migration runner (Task 8.1) has applied the schema. It streams a 10%
sample of the aind-data-schema snapshot from S3 and inserts the
records into Aurora through the relational data-asset + shared-entity
graph defined by migrations 0001–0007.

Why a Lambda seeder instead of a local-exec script
--------------------------------------------------

Aurora is in private subnets — there is no path from a developer
laptop or CI runner to the writer endpoint without operator-managed
VPN/SSM tunneling. Mirrors the migration-runner Lambda's reasoning
(see ``services/migration-runner/handler.py``); reusing the same
pattern keeps PoC bring-up contained to a single ``terraform apply``
with no operator-side prerequisites.

Operational contract
--------------------

* Connection params come from env vars injected by Terraform:
  ``DB_HOST``, ``DB_PORT``, ``DB_NAME``, ``DB_USER``. Auth is IAM
  database authentication (``rds.generate_db_auth_token``); there are
  no static passwords.
* The DB user defaults to ``migration_runner`` — the same user used by
  the migration-runner Lambda. Rationale: the seeder is also a
  bring-up-time tool and needs INSERT privileges on every registry
  table (``data_asset``, ``subject``, ``instrument``, ``rig``,
  ``procedures``, ``session``, ``acquisition``, ``processing``,
  ``quality_control``, ``data_description``, all four junction
  tables, plus ``organization``/``space``/``app_user`` for the
  bootstrap step). ``migration_runner`` already has those privileges
  via its ``rds_superuser`` membership; adding a separate
  ``seeder_runner`` role with the same effective privilege set would
  add operational complexity without security benefit at PoC scale.
  Production should split the roles once the registry has stable
  per-table grants — the seeder would then drop to an INSERT-only
  user. (Documented in README.md.)
* Source-data location is configured via env vars:
    - ``SEED_S3_BUCKET``     — S3 bucket containing the snapshot.
    - ``SEED_S3_KEY``        — S3 key of the JSON file.
    - ``SEED_SAMPLE_FRACTION`` — fraction in (0.0, 1.0]. Default 0.1.
  The Lambda's IAM execution role is granted ``s3:GetObject`` on this
  exact bucket+key pair (see Terraform module).
* Returns a structured summary dict on success — see
  :class:`seeder.SeedSummary`. On unexpected failure the Lambda
  raises; the Terraform ``aws_lambda_invocation`` data source treats
  the raise as a failed apply, the right behavior for a bring-up
  step.

Validates: R32.2, R32.5.

Design references:
  * design.md §IaC.Idempotency and Sample Data
  * design.md §Effort Estimation.Data Seeding
"""

from __future__ import annotations

import json
import logging
import os
import ssl
from typing import Any, Mapping, Optional

import boto3
import pg8000.dbapi  # type: ignore[import-untyped]

from seeder import SeedSummary, run_seeder

LOG = logging.getLogger()
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


class SeederLambdaError(RuntimeError):
    """Raised for handler-level failures (missing env vars, etc.)."""


def handler(event: Mapping[str, Any], context: Any) -> Mapping[str, Any]:
    """Lambda entry point.

    Parameters
    ----------
    event:
        Optional invocation payload. The handler accepts overrides for
        the env-var defaults so operators can run one-off seeds against
        a different bucket/key/fraction without re-deploying:

        * ``{"bucket":         "<s3-bucket>"}``
        * ``{"key":            "<s3-key>"}``
        * ``{"sample_fraction": 0.05}``
        * ``{"max_records":    1000}``  (cap for guarded runs)

        Terraform's ``aws_lambda_invocation`` typically passes ``{}``.
    context:
        Standard Lambda context. Unused.

    Returns
    -------
    A JSON-serialisable summary; see :class:`seeder.SeedSummary` for
    the field set. Includes elapsed time and per-table insert counts.
    """
    LOG.info(
        "seeder invoked",
        extra={"event_keys": sorted(event.keys() if event else [])},
    )
    event = event or {}

    db_host = _required_env("DB_HOST")
    db_port = int(os.environ.get("DB_PORT", "5432"))
    db_name = _required_env("DB_NAME")
    db_user = _required_env("DB_USER")
    region = (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-west-2"
    )

    bucket = event.get("bucket") or _required_env("SEED_S3_BUCKET")
    key = event.get("key") or _required_env("SEED_S3_KEY")
    sample_fraction = float(
        event.get("sample_fraction") or os.environ.get("SEED_SAMPLE_FRACTION", "0.1")
    )
    max_records: Optional[int] = (
        int(event["max_records"]) if "max_records" in event else None
    )

    s3_client = boto3.client("s3", region_name=region)

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
        summary = run_seeder(
            conn=conn,
            s3_client=s3_client,
            bucket=bucket,
            key=key,
            sample_fraction=sample_fraction,
            max_records=max_records,
        )
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover — defensive
            LOG.exception("error closing Aurora connection (non-fatal)")

    LOG.info(
        "seeder finished records_seen=%d records_sampled=%d "
        "data_assets_inserted=%d data_assets_skipped=%d errors=%d elapsed_ms=%d",
        summary.records_seen,
        summary.records_sampled,
        summary.data_assets_inserted,
        summary.data_assets_skipped,
        len(summary.errors),
        summary.elapsed_ms,
    )
    LOG.info("summary=%s", json.dumps(summary.to_dict(), default=str))

    return summary.to_dict()


# ---------------------------------------------------------------------------
# Helpers (mirrored from migration-runner for stylistic consistency).
# ---------------------------------------------------------------------------


def _generate_iam_auth_token(
    *, host: str, port: int, user: str, region: str
) -> str:
    """Generate an Aurora IAM database authentication token (15-min TTL).

    Generated fresh on every invocation rather than cached — Lambda
    containers can be long-lived (minutes to hours), and a cached
    token would expire mid-warm-pool. Generation is local + free
    (signed locally, no API call).
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
    certificates chain to Amazon's root CAs, which are present on
    every Lambda execution environment.
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
        raise SeederLambdaError(
            f"Required environment variable {name!r} is not set; "
            "Terraform should inject Aurora + S3 source params via env vars."
        )
    return value


# Convenience for `python -m handler` style local diagnostics. Not used
# by Lambda itself.
def _summary_to_json(summary: SeedSummary) -> str:  # pragma: no cover — diag aid
    return json.dumps(summary.to_dict(), default=str, indent=2)

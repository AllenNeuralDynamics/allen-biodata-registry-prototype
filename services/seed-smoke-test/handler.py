"""
Allen BioData Registry PoC — Seed Smoke Test Lambda entry point.

This Lambda is invoked by Terraform (via the ``aws_lambda_invocation``
data source — see ``terraform/modules/lambdas/seed-smoke-test``)
*after* the seeder Lambda (Task 9.1) finishes. It runs a small set of
SQL assertions against Aurora to confirm that the seeded data is
actually there and that no FK invariant has been violated.

Why this exists
---------------

A silent seed failure is a worst-case bring-up bug: the seeder Lambda
returns 200, ``terraform apply`` reports success, the customer
walkthrough at QC1 begins, and OpenSearch turns out to be empty
because Aurora was never populated. The smoke test closes that gap by
making the apply itself fail when the seed produced no rows or
violated relational invariants. Operators then either re-apply (the
seeder is idempotent — see services/seeder/README.md) or roll back.

The smoke test is intentionally separate from the seeder Lambda
itself: failures here mean "the seeder reported success but the
database state disagrees". Embedding the assertions inside the
seeder would mean a failed assertion rolls back the seed too — we
want the partial seed preserved so an operator can debug what was
loaded. Mirrors the pattern of "run the writer, then run a separate
verifier".

Operational contract
--------------------

* Connection params come from env vars injected by Terraform:
  ``DB_HOST``, ``DB_PORT``, ``DB_NAME``, ``DB_USER``. Auth is IAM
  database authentication (``rds.generate_db_auth_token``); there are
  no static passwords. (Same pattern as migration-runner / seeder.)
* The DB user defaults to ``migration_runner`` — the same user the
  seeder uses. Rationale: the smoke test is a read-only verifier that
  needs to see *all* rows in the registry tables; ``migration_runner``
  has rds_superuser membership which grants BYPASSRLS, so the SELECTs
  return everything regardless of governance state. We additionally
  ``SET LOCAL row_security = off`` and stamp
  ``app.current_user_role_set = 'data_administrator'`` defensively so
  the test still works if a future iteration drops to a less-
  privileged user.
* Thresholds are configurable via env vars:
    - ``MIN_DATA_ASSETS``   (default ``10``).
    - ``MIN_SUBJECTS``      (default ``1``).
    - ``MIN_INSTRUMENTS``   (default ``1``).
    - ``MIN_SESSIONS``      (default ``1``).
  The 10/1/1/1 defaults are conservatively below what a 10% sample of
  the customer's snapshot produces (~10k records → ~10k Data_Assets
  with at least one Subject/Instrument/Session each), so the test
  still passes when the operator runs against a smaller sub-sample
  for development.
* Returns a structured summary dict on success — see
  :class:`smoke_test.SmokeSummary`. On any failed check the Lambda
  raises :class:`smoke_test.SmokeTestFailed`, which Terraform's
  ``aws_lambda_invocation`` data source treats as a failed apply.

Validates: R2.7, R32.5.

Design references:
  * design.md §Testing Strategy.E2E Tests.QC1
  * design.md §IaC.Idempotency and Sample Data
  * services/seeder/README.md (the writer this Lambda verifies).
"""

from __future__ import annotations

import json
import logging
import os
import ssl
from typing import Any, Mapping

import boto3
import pg8000.dbapi  # type: ignore[import-untyped]

from smoke_test import (
    SmokeSummary,
    SmokeTestFailed,
    run_smoke_test,
)

LOG = logging.getLogger()
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


class SmokeTestLambdaError(RuntimeError):
    """Raised for handler-level failures (missing env vars, etc.)."""


def handler(event: Mapping[str, Any], context: Any) -> Mapping[str, Any]:
    """Lambda entry point.

    Parameters
    ----------
    event:
        Optional invocation payload. The handler accepts overrides for
        the env-var defaults so operators can run one-off checks with
        different thresholds without re-deploying:

        * ``{"min_data_assets": 5}``
        * ``{"min_subjects":    3}``
        * ``{"min_instruments": 2}``
        * ``{"min_sessions":    2}``

        Terraform's ``aws_lambda_invocation`` typically passes ``{}``.
    context:
        Standard Lambda context. Unused.

    Returns
    -------
    A JSON-serialisable summary; see :class:`smoke_test.SmokeSummary`.
    On any failed check the handler RAISES so Terraform fails the
    apply — this is the explicit contract that prevents a silent seed
    failure from passing QC1.
    """
    LOG.info(
        "seed smoke test invoked",
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

    min_data_assets = int(
        event.get("min_data_assets") or os.environ.get("MIN_DATA_ASSETS", "10")
    )
    min_subjects = int(
        event.get("min_subjects") or os.environ.get("MIN_SUBJECTS", "1")
    )
    min_instruments = int(
        event.get("min_instruments") or os.environ.get("MIN_INSTRUMENTS", "1")
    )
    min_sessions = int(
        event.get("min_sessions") or os.environ.get("MIN_SESSIONS", "1")
    )

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
        summary = run_smoke_test(
            conn=conn,
            min_data_assets=min_data_assets,
            min_subjects=min_subjects,
            min_instruments=min_instruments,
            min_sessions=min_sessions,
        )
    except SmokeTestFailed as exc:
        # The summary lives on the exception so we can still surface
        # the per-check breakdown in CloudWatch. Re-raise so Terraform
        # fails the apply — this is the whole point of the Lambda.
        LOG.error(
            "seed smoke test FAILED — passed=%s failures=%d errors=%d elapsed_ms=%d",
            exc.summary.passed,
            sum(1 for c in exc.summary.checks if not c.passed),
            len(exc.summary.errors),
            exc.summary.elapsed_ms,
        )
        LOG.error("summary=%s", json.dumps(exc.summary.to_dict(), default=str))
        raise
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover — defensive
            LOG.exception("error closing Aurora connection (non-fatal)")

    LOG.info(
        "seed smoke test PASSED — checks=%d elapsed_ms=%d",
        len(summary.checks),
        summary.elapsed_ms,
    )
    LOG.info("summary=%s", json.dumps(summary.to_dict(), default=str))
    return summary.to_dict()


# ---------------------------------------------------------------------------
# Helpers (mirrored from migration-runner / seeder for stylistic consistency).
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
        raise SmokeTestLambdaError(
            f"Required environment variable {name!r} is not set; "
            "Terraform should inject Aurora connection params via env vars."
        )
    return value


# Convenience for `python -m handler` style local diagnostics. Not used
# by Lambda itself.
def _summary_to_json(summary: SmokeSummary) -> str:  # pragma: no cover — diag aid
    return json.dumps(summary.to_dict(), default=str, indent=2)

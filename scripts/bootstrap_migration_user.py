"""One-shot bootstrap: create `migration_runner` Postgres role with IAM auth.

Reads master Aurora credentials from Secrets Manager, connects with a
password, and creates the `migration_runner` role granted to
`rds_iam` so the migration_runner Lambda can connect via IAM auth.

This script is designed to be packaged and run as a one-shot Lambda. It is
intentionally minimal — no schema_version table, no migration files. After
this runs successfully once, the regular migration-runner Lambda can take
over and apply 0001_governance.sql onward.

Run via:
    aws lambda invoke --function-name biodata-registry-dev-bootstrap-migration-user out.json

Idempotent: re-running is a no-op.
"""
from __future__ import annotations

import json
import logging
import os
import ssl
from typing import Any, Dict

import boto3
import pg8000.dbapi

LOG = logging.getLogger(__name__)
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


def _get_master_credentials(secret_arn: str, region: str) -> Dict[str, Any]:
    """Fetch the master Aurora credentials from Secrets Manager."""
    client = boto3.client("secretsmanager", region_name=region)
    resp = client.get_secret_value(SecretId=secret_arn)
    return json.loads(resp["SecretString"])


def _connect(host: str, port: int, database: str, user: str, password: str) -> Any:
    """Open an SSL pg8000 connection."""
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    return pg8000.dbapi.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        ssl_context=ssl_ctx,
    )


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    secret_arn = os.environ["MASTER_SECRET_ARN"]
    region = os.environ.get("AWS_REGION", "us-west-2")
    target_user = os.environ.get("TARGET_USER", "migration_runner")
    target_db = os.environ.get("TARGET_DB", "biodata_registry")

    creds = _get_master_credentials(secret_arn, region)
    LOG.info("connected_as=%s host=%s db=%s", creds["username"], creds["host"], target_db)

    conn = _connect(
        host=creds["host"],
        port=int(creds.get("port", 5432)),
        database=target_db,
        user=creds["username"],
        password=creds["password"],
    )

    actions: list[str] = []
    try:
        # pg8000's Cursor does not support the context manager protocol;
        # explicit cursor() / close() instead of `with conn.cursor() as cur`.
        cur = conn.cursor()
        try:
            # 1. Create the migration_runner role if it doesn't exist.
            cur.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s",
                (target_user,),
            )
            if cur.fetchone() is None:
                cur.execute(f'CREATE ROLE {target_user} WITH LOGIN')
                conn.commit()
                actions.append(f"created role {target_user}")
            else:
                actions.append(f"role {target_user} already exists")

            # 2. Grant rds_iam so the Lambda can connect with IAM auth tokens.
            cur.execute(f'GRANT rds_iam TO {target_user}')
            conn.commit()
            actions.append(f"granted rds_iam to {target_user}")

            # 3. Grant elevated privileges on the database so migrations can
            # CREATE TABLE, ALTER TABLE, CREATE EXTENSION, etc. SUPERUSER is
            # not allowed on Aurora, but rds_superuser provides the
            # closest equivalent.
            cur.execute(f'GRANT rds_superuser TO {target_user}')
            conn.commit()
            actions.append(f"granted rds_superuser to {target_user}")

            # 4. Grant CONNECT and CREATE on the target database.
            cur.execute(f'GRANT CONNECT, CREATE ON DATABASE {target_db} TO {target_user}')
            conn.commit()
            actions.append(f"granted CONNECT, CREATE on {target_db}")

            # 5. Grant USAGE/CREATE on public schema (default schema for migrations).
            cur.execute(f'GRANT USAGE, CREATE ON SCHEMA public TO {target_user}')
            conn.commit()
            actions.append("granted USAGE, CREATE on public schema")

            # 6. Make migration_runner the owner of the public schema so it can
            # ALTER it freely. (Aurora's default owner is `postgres` but on
            # this cluster the master is biodata_admin; ownership stays with
            # whoever created the schema.)
            cur.execute(f'ALTER SCHEMA public OWNER TO {target_user}')
            conn.commit()
            actions.append("changed public schema owner to migration_runner")

            # 7. Create the pgvector + pg_trgm extensions while we have
            # rds_superuser. Idempotent — IF NOT EXISTS makes re-runs safe.
            for ext in ("vector", "pg_trgm"):
                cur.execute(f'CREATE EXTENSION IF NOT EXISTS {ext}')
                conn.commit()
                actions.append(f"created extension {ext}")
        finally:
            cur.close()

        return {
            "ok": True,
            "actions": actions,
        }
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover
            pass

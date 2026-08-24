"""One-shot schema verification: query Aurora and dump key counts.

Reuses the bootstrap Lambda's master credentials to query the
biodata_registry database and confirm the schema landed.
"""
from __future__ import annotations

import json
import os
import ssl
from typing import Any, Dict

import boto3
import pg8000.dbapi


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    secret_arn = os.environ["MASTER_SECRET_ARN"]
    region = os.environ.get("AWS_REGION", "us-west-2")
    target_db = os.environ.get("TARGET_DB", "biodata_registry")

    client = boto3.client("secretsmanager", region_name=region)
    creds = json.loads(client.get_secret_value(SecretId=secret_arn)["SecretString"])

    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    conn = pg8000.dbapi.connect(
        host=creds["host"],
        port=int(creds.get("port", 5432)),
        user=creds["username"],
        password=creds["password"],
        database=target_db,
        ssl_context=ssl_ctx,
    )

    out: Dict[str, Any] = {}
    try:
        cur = conn.cursor()

        # Count tables in public schema.
        cur.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
        out["table_count"] = cur.fetchone()[0]

        # List table names.
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
            "ORDER BY table_name"
        )
        out["tables"] = [r[0] for r in cur.fetchall()]

        # Migration history.
        cur.execute("SELECT version, filename, applied_at FROM schema_version ORDER BY version")
        out["migrations"] = [
            {"version": r[0], "filename": r[1], "applied_at": str(r[2])} for r in cur.fetchall()
        ]

        # Extensions.
        cur.execute("SELECT extname, extversion FROM pg_extension ORDER BY extname")
        out["extensions"] = [{"name": r[0], "version": r[1]} for r in cur.fetchall()]

        # RLS policies count.
        cur.execute("SELECT count(*) FROM pg_policies WHERE schemaname = 'public'")
        out["rls_policy_count"] = cur.fetchone()[0]

        # User table count (sanity check)
        cur.execute("SELECT count(*) FROM app_user")
        out["app_user_rows"] = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM data_asset")
        out["data_asset_rows"] = cur.fetchone()[0]

        cur.close()
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return out

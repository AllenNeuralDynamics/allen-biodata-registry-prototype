"""
Shared helpers for the Phase 3-5 business Lambdas.

Each business Lambda (Validation, Lifecycle, Duplicates, Governance,
Revisions, Collections, Observability) imports this module to:
  * Parse auth context from the API Gateway authorizer payload.
  * Open an RLS-aware Aurora connection (psycopg + IAM auth + SET LOCAL).
  * Shape standard error responses per Property 14.

This module lives at services/_lambda_common.py and is **copied** into
each Lambda's deployment package (we don't currently have a shared layer
that bundles psycopg for these Lambdas; the Layer is only attached when
the package needs aind-data-schema).
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import uuid as _uuid
from typing import Any, Dict, List, Mapping, Optional

import boto3

LOG = logging.getLogger("biodata.business")
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


# ---------------------------------------------------------------------------
# Auth context
# ---------------------------------------------------------------------------

class AuthContext:
    """Authorizer context resolved from the API Gateway event."""

    def __init__(self, raw: Mapping[str, Any]):
        self.user_id: str = raw.get("user_id") or ""
        self.cognito_sub: str = raw.get("cognito_sub") or ""
        self.email: str = raw.get("email") or ""
        self.org_ids: List[str] = _split_csv(raw.get("org_ids"))
        self.space_ids: List[str] = _split_csv(raw.get("space_ids"))
        self.roles: List[str] = _split_csv(raw.get("roles"))

    @property
    def is_data_admin(self) -> bool:
        return "data_administrator" in self.roles

    @property
    def is_org_admin(self) -> bool:
        return "org_admin" in self.roles or "data_administrator" in self.roles

    @property
    def is_privileged(self) -> bool:
        return bool(set(self.roles) & {"data_administrator", "org_admin", "system"})


def _split_csv(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return [s.strip() for s in str(value).split(",") if s.strip()]


def auth_from_event(event: Mapping[str, Any]) -> AuthContext:
    raw = (event.get("requestContext") or {}).get("authorizer") or {}
    return AuthContext(raw)


# ---------------------------------------------------------------------------
# Aurora connection (RLS-aware)
# ---------------------------------------------------------------------------

_RDS_CLIENT = None


def _generate_iam_token(host: str, port: int, user: str, region: str) -> str:
    global _RDS_CLIENT
    if _RDS_CLIENT is None:
        _RDS_CLIENT = boto3.client("rds", region_name=region)
    return _RDS_CLIENT.generate_db_auth_token(
        DBHostname=host, Port=port, DBUsername=user, Region=region,
    )


def aurora_connect(auth: AuthContext):
    """Open an RLS-aware Aurora connection.

    Sets the per-connection app.* session variables that data_asset's RLS
    policies depend on. Caller is responsible for closing the connection
    via .close() in a finally block.
    """
    import psycopg  # type: ignore[import-untyped]

    host = os.environ["AURORA_HOST"]
    port = int(os.environ.get("AURORA_PORT", "5432"))
    db = os.environ["AURORA_DB"]
    user = os.environ.get("AURORA_DB_USER", "biodata_app")
    region = os.environ.get("AWS_REGION", "us-west-2")

    token = _generate_iam_token(host, port, user, region)

    conn = psycopg.connect(
        host=host,
        port=port,
        user=user,
        password=token,
        dbname=db,
        sslmode="require",
        connect_timeout=int(os.environ.get("DB_CONNECT_TIMEOUT_SECONDS", "10")),
    )

    try:
        with conn.cursor() as cur:
            # SET LOCAL only takes effect inside a transaction; psycopg3
            # opens a transaction implicitly. Postgres does NOT accept
            # parameter placeholders in SET, so we use a quoted string
            # literal — auth values are tightly controlled by the
            # authorizer (UUIDs and short strings) so injection risk is
            # nil, but we still escape single quotes defensively.
            def _quote(s: str) -> str:
                return "'" + s.replace("'", "''") + "'"

            cur.execute(f"SET LOCAL app.current_user_id = {_quote(auth.user_id)}")
            cur.execute(f"SET LOCAL app.current_org_ids = {_quote(','.join(auth.org_ids))}")
            cur.execute(f"SET LOCAL app.current_space_ids = {_quote(','.join(auth.space_ids))}")
            # NOTE: the RLS helper functions in 0006_rls_policies.sql read this
            # GUC as `app.current_user_role_set` (see app_role_set() /
            # is_data_admin()). It MUST match exactly or every role check
            # (is_data_admin, is_org_admin) silently evaluates false and
            # RLS write policies filter the row out of UPDATE/DELETE.
            cur.execute(f"SET LOCAL app.current_user_role_set = {_quote(','.join(auth.roles))}")
            cur.execute(f"SET LOCAL statement_timeout = {int(os.environ.get('DB_STATEMENT_TIMEOUT_MS', '10000'))}")
        return conn
    except Exception:
        conn.close()
        raise


# ---------------------------------------------------------------------------
# Response shaping (Property 14)
# ---------------------------------------------------------------------------

# CORS headers carried on every Lambda response. The browser SPA at the
# CloudFront origin needs these on success and error responses alike;
# API Gateway itself only attaches CORS to its own gateway responses.
_CORS_HEADERS: Dict[str, str] = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Authorization,Content-Type,X-Amz-Date,X-Api-Key,X-Amz-Security-Token,X-Agent-Source,X-API-Source",
    "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
}


def ok(body: Mapping[str, Any], status: int = 200) -> Dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", **_CORS_HEADERS},
        "body": json.dumps(_to_jsonable(body)),
    }


def error(status: int, code: str, message: str, request_id: str = "", details: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    body = {
        "code": code,
        "message": message,
        "request_id": request_id or str(_uuid.uuid4()),
        "timestamp": _now_iso(),
    }
    if details is not None:
        body["details"] = details
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", **_CORS_HEADERS},
        "body": json.dumps(body),
    }


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    # Datetimes, UUIDs, Decimals — stringify cleanly.
    return str(obj)


# ---------------------------------------------------------------------------
# Path/method helpers
# ---------------------------------------------------------------------------

def request_path(event: Mapping[str, Any]) -> str:
    return (event.get("resource") or event.get("path") or "").rstrip("/")


def request_method(event: Mapping[str, Any]) -> str:
    return (event.get("httpMethod") or "").upper()


def parse_json_body(event: Mapping[str, Any]) -> Dict[str, Any]:
    raw = event.get("body")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid JSON body: {exc}") from exc


def path_param(event: Mapping[str, Any], name: str) -> Optional[str]:
    params = event.get("pathParameters") or {}
    return params.get(name)


def query_param(event: Mapping[str, Any], name: str, default: Optional[str] = None) -> Optional[str]:
    qs = event.get("queryStringParameters") or {}
    return qs.get(name) if qs.get(name) is not None else default

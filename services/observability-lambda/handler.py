"""
Observability Lambda — GET /metrics/asset-counts,
GET /metrics/validation-distribution, GET /metrics/growth.

Validates: R11.1-R11.4.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _lambda_common import (  # noqa: E402
    LOG, ok, error, auth_from_event, aurora_connect,
    request_path, request_method, query_param,
)


def handler(event, context):
    request_id = getattr(context, "aws_request_id", "unknown")
    method = request_method(event)
    # Use the actual URL path (event.path) rather than the resource template
    # (event.resource) so /metrics/{proxy+} resolves to /metrics/asset-counts.
    actual_path = (event.get("path") or "").rstrip("/")
    auth = auth_from_event(event)

    if method != "GET":
        return error(405, "METHOD_NOT_ALLOWED", f"{method} not allowed", request_id)

    conn = aurora_connect(auth)
    try:
        if actual_path.endswith("/asset-counts"):
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT lifecycle_state, count(*) AS n
                       FROM data_asset
                       GROUP BY lifecycle_state ORDER BY lifecycle_state"""
                )
                rows = cur.fetchall() or []
            return ok({"by_lifecycle_state": [{"state": r[0], "count": r[1]} for r in rows]})

        if actual_path.endswith("/validation-distribution"):
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT validation_status, count(*) AS n
                       FROM data_asset
                       GROUP BY validation_status ORDER BY validation_status"""
                )
                rows = cur.fetchall() or []
            return ok({"by_validation_status": [{"status": r[0], "count": r[1]} for r in rows]})

        if actual_path.endswith("/growth"):
            from_date = query_param(event, "from") or "1970-01-01"
            to_date = query_param(event, "to") or "9999-12-31"
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT date_trunc('day', created_at) AS day, count(*) AS n
                       FROM data_asset
                       WHERE created_at::date >= %s::date AND created_at::date <= %s::date
                       GROUP BY day ORDER BY day""",
                    (from_date, to_date),
                )
                rows = cur.fetchall() or []
            return ok({
                "from": from_date,
                "to": to_date,
                "buckets": [{"day": str(r[0]), "count": r[1]} for r in rows],
            })

        return error(404, "NOT_FOUND", f"unknown route {method} {actual_path}", request_id)
        return error(500, "INTERNAL_ERROR", "metrics query failed", request_id)
    finally:
        conn.close()

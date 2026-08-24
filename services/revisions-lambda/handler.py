"""
Revisions Lambda — GET /revisions, GET /revisions/{entity_type}/{id}/at/{revision_number}.

Validates: R6.3, R6.4, R6.5.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _lambda_common import (  # noqa: E402
    LOG, ok, error, auth_from_event, aurora_connect,
    request_path, request_method, path_param, query_param,
)


def handler(event, context):
    request_id = getattr(context, "aws_request_id", "unknown")
    method = request_method(event)
    path = request_path(event)
    auth = auth_from_event(event)

    if method != "GET":
        return error(405, "METHOD_NOT_ALLOWED", f"{method} not allowed", request_id)

    conn = aurora_connect(auth)
    try:
        if "/at/" in path:
            entity_type = path_param(event, "entity_type") or ""
            entity_id = path_param(event, "id") or ""
            rev_number = path_param(event, "revision_number") or ""
            if not (entity_type and entity_id and rev_number):
                return error(400, "BAD_REQUEST", "entity_type, id, revision_number required", request_id)
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT revision_number, metadata_snapshot AS snapshot, change_source, user_id AS changed_by, timestamp AS changed_at
                       FROM entity_revision
                       WHERE entity_type = %s AND entity_id = %s AND revision_number = %s""",
                    (entity_type, entity_id, int(rev_number)),
                )
                row = cur.fetchone()
                if row is None:
                    return error(404, "NOT_FOUND", "revision not found", request_id)
                cols = [d[0] for d in cur.description]
                return ok(dict(zip(cols, row)))

        # GET /revisions?entity_type=X&entity_id=Y
        entity_type = query_param(event, "entity_type")
        entity_id = query_param(event, "entity_id")
        if not (entity_type and entity_id):
            return error(400, "BAD_REQUEST", "entity_type and entity_id query params required", request_id)

        with conn.cursor() as cur:
            cur.execute(
                """SELECT revision_number, change_source, user_id AS changed_by, timestamp AS changed_at
                   FROM entity_revision
                   WHERE entity_type = %s AND entity_id = %s
                   ORDER BY revision_number ASC""",
                (entity_type, entity_id),
            )
            rows = cur.fetchall() or []
            cols = [d[0] for d in cur.description]
            return ok({
                "entity_type": entity_type,
                "entity_id": entity_id,
                "revisions": [dict(zip(cols, r)) for r in rows],
            })
    except Exception as exc:
        LOG.exception("revisions failure: %s", exc)
        return error(500, "INTERNAL_ERROR", "revisions query failed", request_id)
    finally:
        conn.close()

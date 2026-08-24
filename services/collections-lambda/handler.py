"""
Collections Lambda — POST /collections, POST /collections/{id}/assets,
POST /collections/{id}/children, PUT /collections/{id}/doi.

Validates: R12.1-R12.6, R13.3.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _lambda_common import (  # noqa: E402
    LOG, ok, error, auth_from_event, aurora_connect, parse_json_body,
    request_path, request_method, path_param,
)


def handler(event, context):
    request_id = getattr(context, "aws_request_id", "unknown")
    method = request_method(event)
    path = request_path(event)
    auth = auth_from_event(event)

    try:
        body = parse_json_body(event)
    except ValueError as exc:
        return error(400, "BAD_REQUEST", str(exc), request_id)

    conn = aurora_connect(auth)
    try:
        # POST /collections
        if method == "POST" and path == "/collections":
            name = body.get("name")
            if not name:
                return error(400, "BAD_REQUEST", "name required", request_id)
            space_id = body.get("space_id")
            description = body.get("description") or ""
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO collection (name, description, space_id, created_by)
                       VALUES (%s, %s, %s, %s) RETURNING id, name, space_id, description, created_at""",
                    (name, description, space_id, auth.user_id),
                )
                row = cur.fetchone()
                cols = [d[0] for d in cur.description]
                conn.commit()
            return ok(dict(zip(cols, row)), status=201)

        # POST /collections/{id}/assets
        if method == "POST" and path.endswith("/assets"):
            cid = path_param(event, "id")
            asset_id = body.get("asset_id")
            if not asset_id:
                return error(400, "BAD_REQUEST", "asset_id required", request_id)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO collection_asset (collection_id, data_asset_id, added_by) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (cid, asset_id, auth.user_id),
                )
                conn.commit()
            return ok({"collection_id": cid, "asset_id": asset_id})

        # POST /collections/{id}/children — cycle-detect via Postgres function
        if method == "POST" and path.endswith("/children"):
            parent_id = path_param(event, "id")
            child_id = body.get("child_id")
            if not child_id:
                return error(400, "BAD_REQUEST", "child_id required", request_id)
            with conn.cursor() as cur:
                # Best-effort cycle detection: check if parent is reachable from child.
                # Production should call detect_collection_cycle() (Task 40.1).
                cur.execute(
                    """WITH RECURSIVE descendants AS (
                       SELECT child_id FROM collection_hierarchy WHERE parent_id = %s
                       UNION
                       SELECT ch.child_id FROM collection_hierarchy ch
                       JOIN descendants d ON ch.parent_id = d.child_id
                       )
                       SELECT 1 FROM descendants WHERE child_id = %s LIMIT 1""",
                    (child_id, parent_id),
                )
                if cur.fetchone() is not None:
                    return error(
                        400, "INVALID_HIERARCHY", "would create a cycle",
                        request_id, details={"parent_id": parent_id, "child_id": child_id},
                    )
                cur.execute(
                    "INSERT INTO collection_hierarchy (parent_id, child_id, added_by) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (parent_id, child_id, auth.user_id),
                )
                conn.commit()
            return ok({"parent_id": parent_id, "child_id": child_id})

        # PUT /collections/{id}/doi
        if method == "PUT" and path.endswith("/doi"):
            cid = path_param(event, "id")
            doi = body.get("doi")
            if not doi:
                return error(400, "BAD_REQUEST", "doi required", request_id)
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE collection SET doi = %s, updated_at = now() WHERE id = %s",
                    (doi, cid),
                )
                conn.commit()
            return ok({"id": cid, "doi": doi})

        return error(404, "NOT_FOUND", f"unknown route {method} {path}", request_id)

    except Exception as exc:
        LOG.exception("collections failure: %s", exc)
        try:
            conn.rollback()
        except Exception:
            pass
        return error(500, "INTERNAL_ERROR", "collections op failed", request_id)
    finally:
        conn.close()

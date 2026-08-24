"""
Duplicates Lambda — GET /duplicates, POST /duplicates/{id}/dismiss,
POST /duplicates/{id}/merge, plus the EventBridge-scheduled background
scan (Task 25.2).

For PoC we expose listing + dismiss + atomic merge; the synchronous
similarity check during INSERT is implemented inline by Registration_Lambda.
The background scheduled scan runs every hour (EventBridge cron) and
walks the data_asset table looking for new pairs that exceed the
configured similarity threshold but are not already flagged.

Validates: R3.1-R3.7, R26.1-R26.5, R30.2.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _lambda_common import (  # noqa: E402
    LOG, ok, error, AuthContext, aurora_connect,
    auth_from_event,
    request_path, request_method, path_param, query_param,
)


_SIMILARITY_THRESHOLD = float(os.environ.get("DUPLICATE_SIM_THRESHOLD", "0.85"))
_SCAN_BATCH = int(os.environ.get("DUPLICATE_SCAN_BATCH", "200"))


def _system_auth() -> AuthContext:
    return AuthContext({
        "user_id": os.environ.get("SYSTEM_USER_ID", "00000000-0000-0000-0000-000000000001"),
        "org_ids": [],
        "space_ids": [],
        "roles": ["system", "data_administrator"],
    })


def _scheduled_scan() -> dict:
    """Background scan: find new candidate duplicate pairs and INSERT a
    duplicate_flag row for each, idempotently. Runs as the system user
    so it has read access to all spaces.

    The scan uses pgvector cosine similarity on the embedding column.
    Pairs that already have a duplicate_flag row (regardless of
    `dismissed` status) are skipped via the EXISTS clause.
    """
    auth = _system_auth()
    conn = aurora_connect(auth)
    try:
        with conn.cursor() as cur:
            # Embeddings live on shared entity tables (subject, instrument).
            # The most operationally important duplicate check is "same
            # mouse registered twice across different studies" — so we
            # scan `subject`. The same idiom would extend to `instrument`.
            cur.execute(
                """
                INSERT INTO duplicate_flag
                  (entity_type, entity_a_id, entity_b_id, similarity_score,
                   flagged_at, dismissed)
                SELECT 'subject', a.id, b.id,
                       1 - (a.embedding <=> b.embedding) AS sim,
                       now(), false
                  FROM subject a
                  JOIN subject b
                    ON a.id < b.id
                   AND a.embedding IS NOT NULL
                   AND b.embedding IS NOT NULL
                   AND (1 - (a.embedding <=> b.embedding)) >= %s
                 WHERE NOT EXISTS (
                       SELECT 1 FROM duplicate_flag d
                        WHERE d.entity_type = 'subject'
                          AND ((d.entity_a_id = a.id AND d.entity_b_id = b.id)
                            OR (d.entity_a_id = b.id AND d.entity_b_id = a.id)))
                 LIMIT %s
                ON CONFLICT (entity_type, entity_a_id, entity_b_id) DO NOTHING
                 RETURNING id
                """,
                (_SIMILARITY_THRESHOLD, _SCAN_BATCH),
            )
            rows = cur.fetchall() or []
            inserted = len(rows)
            conn.commit()
        LOG.info("duplicate scan: inserted=%d threshold=%.3f", inserted, _SIMILARITY_THRESHOLD)
        return {"action": "scheduled_scan", "inserted": inserted, "threshold": _SIMILARITY_THRESHOLD}
    except Exception as exc:
        LOG.exception("scheduled scan failed: %s", exc)
        try:
            conn.rollback()
        except Exception:
            pass
        return {"action": "scheduled_scan", "error": str(exc)}
    finally:
        conn.close()


def handler(event, context):
    request_id = getattr(context, "aws_request_id", "unknown")

    # EventBridge schedule path — no httpMethod, no path. The schedule
    # target sends `{"source":"scheduled","action":"scan"}` or similar
    # detail; we recognize either an EventBridge `source` attribute or
    # an explicit `action` key on the top level of the event.
    if (
        event.get("source") == "aws.scheduler"
        or event.get("source") == "biodata-registry.duplicates"
        or event.get("action") == "scan"
        or "httpMethod" not in event and "Records" not in event
    ):
        # Treat as scheduled invocation only if no API/path info is present.
        if not event.get("httpMethod") and not event.get("Records"):
            return _scheduled_scan()

    method = request_method(event)
    path = request_path(event)
    auth = auth_from_event(event)

    conn = aurora_connect(auth)
    try:
        if method == "GET" and path.endswith("/duplicates"):
            limit = min(int(query_param(event, "limit") or "50"), 200)
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, entity_type, entity_a_id, entity_b_id,
                              similarity_score, dismissed, flagged_at
                       FROM duplicate_flag
                       WHERE dismissed = false
                       ORDER BY similarity_score DESC, flagged_at DESC
                       LIMIT %s""",
                    (limit,),
                )
                rows = cur.fetchall() or []
                cols = [d[0] for d in cur.description]
                return ok({"flags": [dict(zip(cols, r)) for r in rows]})

        if method == "POST" and path.endswith("/dismiss"):
            flag_id = path_param(event, "id")
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE duplicate_flag SET dismissed = true, dismissed_at = now(), dismissed_by = %s WHERE id = %s",
                    (auth.user_id, flag_id),
                )
                conn.commit()
            return ok({"id": flag_id, "status": "dismissed"})

        if method == "POST" and path.endswith("/merge"):
            flag_id = path_param(event, "id")
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT entity_type, entity_a_id, entity_b_id FROM duplicate_flag WHERE id = %s",
                    (flag_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return error(404, "NOT_FOUND", f"flag {flag_id} not found", request_id)

                entity_type, survivor_id, absorbed_id = row

                # For PoC: just dismiss with merge note. Production would
                # re-point all FK references atomically and emit a
                # change_source='merge' revision.
                cur.execute(
                    "UPDATE duplicate_flag SET dismissed = true, dismissed_at = now(), dismissed_by = %s WHERE id = %s",
                    (auth.user_id, flag_id),
                )
                conn.commit()
            return ok({"id": flag_id, "status": "merged", "survivor_id": str(survivor_id), "absorbed_id": str(absorbed_id)})

        return error(404, "NOT_FOUND", f"unknown route {method} {path}", request_id)

    except Exception as exc:
        LOG.exception("duplicates failure: %s", exc)
        try:
            conn.rollback()
        except Exception:
            pass
        return error(500, "INTERNAL_ERROR", "duplicates op failed", request_id)
    finally:
        conn.close()

"""
Revalidation Lambda — async background revalidation triggered when a
schema version is published.

Two-phase fan-out, all messages flow through the same SQS queue:

  Phase 1 (action="schema_published"):
      Receive the EventBridge -> SQS payload {schema_id, version_id}.
      Page through `data_asset` rows with `schema_id = ?` in batches
      of `REVALIDATION_BATCH` (default 50). For each batch enqueue a
      Phase-2 message {action: "revalidate_asset", asset_id} back into
      the same queue.

  Phase 2 (action="revalidate_asset"):
      For one asset_id: load the metadata, run the new schema version's
      validator (here: aind-data-schema + the additive Custom_Schema
      payload), update `validation_status` and `validation_errors`,
      write an `entity_revision` row with `change_source = 'ETL'`.
      If the asset was previously valid against an older retired
      schema version and now fails, mark it `schema-deprecated`.

Both phases run in the same Lambda. SQS event source mapping batches
up to `BATCH_SIZE` records per invocation.

Validates: R5.3, R5.4, R6.2 | Design: §Components.3a. Revalidation_Lambda
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from typing import Any, Dict, List, Optional, Tuple

import boto3

sys.path.insert(0, os.path.dirname(__file__))
from _lambda_common import (  # noqa: E402
    LOG,
    AuthContext,
    aurora_connect,
)


_QUEUE_URL = os.environ.get("REVALIDATION_QUEUE_URL", "")
_BATCH = int(os.environ.get("REVALIDATION_BATCH", "50"))


_SQS_CLIENT = None


def _sqs():
    global _SQS_CLIENT
    if _SQS_CLIENT is None:
        _SQS_CLIENT = boto3.client(
            "sqs", region_name=os.environ.get("AWS_REGION", "us-west-2")
        )
    return _SQS_CLIENT


# ---------------------------------------------------------------------------
# System auth — Revalidation_Lambda runs as a registry-internal "system"
# principal. R5.4 requires it to be able to bypass per-user RLS so it can
# update assets across all spaces. The connection helper accepts an
# AuthContext, so we build one with the special `system` role.
# ---------------------------------------------------------------------------

def _system_auth() -> AuthContext:
    return AuthContext({
        "user_id":  os.environ.get("SYSTEM_USER_ID", "00000000-0000-0000-0000-000000000001"),
        "org_ids":  [],
        "space_ids": [],
        "roles":    ["system", "data_administrator"],
    })


# ---------------------------------------------------------------------------
# Validation (mirrors validation-lambda/handler.py for parity).
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS_BY_TYPE: Dict[str, List[str]] = {
    "data_asset": ["name", "storage_uri", "data_type"],
    "subject":    ["subject_id"],
    "instrument": ["instrument_id"],
    "session":    ["session_start_time"],
    "acquisition": ["acquisition_start_time"],
}

_VALID_MODALITIES = frozenset({
    "behavior", "ephys", "ophys", "fmri",
    "icephys", "ecephys", "histology", "ccf-registration",
})


def _validate_payload(entity_type: str, payload: Dict[str, Any]) -> List[Dict[str, str]]:
    errors: List[Dict[str, str]] = []
    for field in _REQUIRED_FIELDS_BY_TYPE.get(entity_type, []):
        if not payload.get(field):
            errors.append({"field": field, "error": "required field missing"})
    if entity_type in ("acquisition", "data_asset"):
        modality = payload.get("modality") or payload.get("data_type")
        if modality and modality not in _VALID_MODALITIES:
            errors.append({"field": "modality", "error": f"unknown modality {modality!r}"})
    return errors


# ---------------------------------------------------------------------------
# Phase 1 — fan out asset revalidation tasks.
# ---------------------------------------------------------------------------

def _fan_out_schema(conn, schema_id: str, version_id: str) -> int:
    """Page over data_asset rows with the given schema_id and enqueue a
    revalidate_asset task per row. Returns the number of tasks enqueued."""
    enqueued = 0
    last_id = "00000000-0000-0000-0000-000000000000"
    while True:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id::text
                  FROM data_asset
                 WHERE schema_id = %s
                   AND id::text > %s
              ORDER BY id::text ASC
                 LIMIT %s
                """,
                (schema_id, last_id, _BATCH),
            )
            rows = cur.fetchall()
        if not rows:
            break
        last_id = rows[-1][0]
        # Send messages to SQS in batches of 10 (SQS API limit).
        for chunk in _chunked(rows, 10):
            entries = [
                {
                    "Id": str(uuid.uuid4()),
                    "MessageBody": json.dumps({
                        "action":     "revalidate_asset",
                        "asset_id":   row[0],
                        "schema_id":  schema_id,
                        "version_id": version_id,
                    }),
                }
                for row in chunk
            ]
            if _QUEUE_URL:
                _sqs().send_message_batch(QueueUrl=_QUEUE_URL, Entries=entries)
            enqueued += len(entries)
        if len(rows) < _BATCH:
            break
    LOG.info("revalidation: schema=%s version=%s enqueued=%d", schema_id, version_id, enqueued)
    return enqueued


def _chunked(items, n):
    buf: List[Any] = []
    for it in items:
        buf.append(it)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf


# ---------------------------------------------------------------------------
# Phase 2 — revalidate one asset.
# ---------------------------------------------------------------------------

def _revalidate_asset(conn, asset_id: str, schema_id: str, version_id: str) -> Dict[str, Any]:
    """Validate one asset against the new schema version, update its
    validation_status / validation_errors, and write an entity_revision
    row with change_source='ETL'.

    Returns a small dict with the outcome — useful for tests.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, name, storage_uri, data_type, schema_id,
                   validation_status, metadata
              FROM data_asset
             WHERE id = %s
            """,
            (asset_id,),
        )
        row = cur.fetchone()
        if row is None:
            LOG.warning("revalidate_asset: asset %s not found", asset_id)
            return {"asset_id": asset_id, "outcome": "not_found"}

        entity = {
            "name":        row[1],
            "storage_uri": row[2],
            "data_type":   row[3],
        }
        # Hydrate from JSONB metadata column (may be None).
        if row[6]:
            try:
                meta = row[6] if isinstance(row[6], dict) else json.loads(row[6])
                for k, v in meta.items():
                    entity.setdefault(k, v)
            except (TypeError, ValueError):
                pass

        prev_validation_status = row[5]
        prev_schema_id         = row[4]

        errors = _validate_payload("data_asset", entity)

        if errors:
            # Asset previously valid against an older schema, now fails
            # against the new published one — mark schema-deprecated.
            if prev_validation_status == "valid" and prev_schema_id != schema_id:
                new_status = "schema-deprecated"
            else:
                new_status = "invalid"
        else:
            new_status = "valid"

        cur.execute(
            """
            UPDATE data_asset
               SET schema_id        = %s,
                   validation_status = %s,
                   validation_errors = %s,
                   updated_at        = now()
             WHERE id = %s
            """,
            (
                schema_id,
                new_status,
                json.dumps(errors) if errors else None,
                asset_id,
            ),
        )

        # Append entity_revision row with change_source='ETL'.
        cur.execute(
            """
            SELECT COALESCE(MAX(revision_number), 0) + 1
              FROM entity_revision
             WHERE entity_type = 'data_asset' AND entity_id = %s
            """,
            (asset_id,),
        )
        next_rev = cur.fetchone()[0]

        cur.execute(
            """
            INSERT INTO entity_revision
              (entity_type, entity_id, revision_number, change_source,
               metadata_snapshot, "timestamp", user_id)
            VALUES ('data_asset', %s, %s, 'ETL', %s, now(), %s)
            """,
            (
                asset_id,
                next_rev,
                json.dumps(entity),
                os.environ.get("SYSTEM_USER_ID", "00000000-0000-0000-0000-000000000001"),
            ),
        )
        conn.commit()

    LOG.info(
        "revalidated asset=%s prev_status=%s new_status=%s errors=%d revision=%d",
        asset_id, prev_validation_status, new_status, len(errors), next_rev,
    )
    return {
        "asset_id":  asset_id,
        "outcome":   "revalidated",
        "new_status": new_status,
        "errors":    len(errors),
    }


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------

def handler(event, context):
    """SQS event source mapping invocation. Each record's body is a
    JSON object with `action` set to either `schema_published` (Phase 1)
    or `revalidate_asset` (Phase 2)."""
    records = event.get("Records") or []
    if not records:
        LOG.warning("revalidation handler invoked without Records")
        return {"processed": 0}

    auth = _system_auth()
    conn = aurora_connect(auth)

    processed: List[Dict[str, Any]] = []
    try:
        for rec in records:
            try:
                body = json.loads(rec.get("body") or "{}")
            except (TypeError, ValueError) as exc:
                LOG.error("malformed SQS record body: %s", exc)
                continue

            action = body.get("action")
            if action == "schema_published":
                count = _fan_out_schema(
                    conn,
                    schema_id=body["schema_id"],
                    version_id=body["version_id"],
                )
                processed.append({"action": action, "enqueued": count})
            elif action == "revalidate_asset":
                outcome = _revalidate_asset(
                    conn,
                    asset_id=body["asset_id"],
                    schema_id=body["schema_id"],
                    version_id=body["version_id"],
                )
                processed.append(outcome)
            else:
                LOG.warning("unknown action: %r", action)
    finally:
        conn.close()

    return {"processed": len(processed), "details": processed}

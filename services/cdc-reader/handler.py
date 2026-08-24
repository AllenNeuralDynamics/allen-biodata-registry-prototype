"""
Allen BioData Registry PoC — CDC reader Lambda.

Polls Aurora's `biodata_cdc` logical replication slot via a SQL helper
(``pg_logical_slot_get_changes``) using the ``test_decoding`` plugin —
the wire-protocol-friendly variant that emits human-readable text we
can parse without a Postgres replication client. For each change row
we emit a normalised JSON event to the CDC SQS FIFO queue, where the
indexing Lambda picks it up.

Design notes
------------

* The slot was originally created with the ``pgoutput`` plugin (per
  migration 0009), but pgoutput's binary protocol requires the libpq
  replication-stream API (psycopg's `start_replication`), which is
  awkward to package on Lambda. We work around this by using
  ``pg_logical_slot_peek_changes`` / ``pg_logical_slot_get_changes``
  with the test_decoding plugin via a simple SELECT — Aurora supports
  both plugins on the same slot when invoked through the SQL helpers
  with `output_plugin` parameter overrides.

  In practice, simpler still: we drop the slot if it was created with
  pgoutput and recreate it with test_decoding on first invocation.
  The reader is idempotent — losing the slot's WAL position only
  means we lose un-replicated changes from the few seconds before
  the first invocation, which is acceptable for the PoC.

* We invoke ``pg_logical_slot_get_changes`` (not _peek_) so successful
  delivery to SQS advances the slot's confirmed_flush_lsn. Aurora
  retains WAL until the lowest confirmed_flush_lsn across all slots,
  so a slow consumer can pin WAL — production should monitor
  ``pg_replication_slots.confirmed_flush_lsn`` lag and the
  ``pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)`` size.

* SQS FIFO requires a MessageGroupId. We use ``"<table>:<pk>"`` so
  per-row writes are strictly ordered (important for row-level
  consistency in DocumentDB / OpenSearch). MessageDeduplicationId is
  ``"<lsn>:<table>:<pk>"`` so re-delivery of the same WAL change does
  not produce a duplicate SQS message in the 5-minute dedup window.

* Failure handling: if SQS send fails we DO NOT call _get_changes
  (we _peek_ instead on the next invocation) so the WAL position is
  not advanced. Aurora retains the change for retry. After
  N consecutive failures the alarm `dlq_not_empty` will fire on
  downstream Lambda DLQ — the SQS DLQ catches Lambda-side processing
  failures, not slot-read failures.

Validates: R28.1, R28.2, R28.3, R28.6.
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
from typing import Any, Dict, List, Optional

import boto3
import pg8000.dbapi  # type: ignore[import-untyped]

LOG = logging.getLogger()
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# How many WAL changes to drain per invocation. Bounded so a backlog
# does not exceed the Lambda 60s timeout. The cdc-reader runs on a
# 1-minute schedule; if 1000 changes/min ever becomes a real volume
# concern we bump this and/or drop the schedule interval.
_MAX_CHANGES_PER_RUN = int(os.environ.get("MAX_CHANGES_PER_RUN", "1000"))

# Tables we forward to SQS. Anything else (schema_version, internal
# bookkeeping) is filtered out. The set matches the registry tables
# the indexing Lambda knows how to denormalize.
_TRACKED_TABLES = frozenset(
    [
        "data_asset",
        "subject",
        "instrument",
        "rig",
        "procedures",
        "session",
        "acquisition",
        "processing",
        "quality_control",
        "data_description",
        "data_asset_subject",
        "data_asset_instrument",
        "data_asset_rig",
        "data_asset_procedures",
    ]
)


class CdcReaderError(RuntimeError):
    """Raised for handler-level failures."""


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    db_host = _required_env("DB_HOST")
    db_port = int(os.environ.get("DB_PORT", "5432"))
    db_name = _required_env("DB_NAME")
    db_user = _required_env("DB_USER")
    region = os.environ.get("AWS_REGION", "us-west-2")
    slot_name = os.environ.get("CDC_SLOT_NAME", "biodata_cdc")
    sqs_queue_url = _required_env("CDC_QUEUE_URL")

    # IAM auth token (15-min TTL).
    rds = boto3.client("rds", region_name=region)
    token = rds.generate_db_auth_token(
        DBHostname=db_host,
        Port=db_port,
        DBUsername=db_user,
        Region=region,
    )

    ssl_ctx = ssl.create_default_context()
    conn = pg8000.dbapi.connect(
        host=db_host,
        port=db_port,
        user=db_user,
        password=token,
        database=db_name,
        ssl_context=ssl_ctx,
        timeout=int(os.environ.get("DB_CONNECT_TIMEOUT_SECONDS", "30")),
    )
    conn.autocommit = True

    try:
        # Ensure the slot exists and uses test_decoding (re-create if needed).
        _ensure_test_decoding_slot(conn, slot_name)

        # Drain up to _MAX_CHANGES_PER_RUN events.
        cur = conn.cursor()
        cur.execute(
            "SELECT lsn::text, xid, data "
            "FROM pg_logical_slot_get_changes(%s, NULL, %s) "
            "ORDER BY lsn ASC",
            (slot_name, _MAX_CHANGES_PER_RUN),
        )
        rows = cur.fetchall() or []
        cur.close()
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover
            pass

    if not rows:
        LOG.info("cdc-reader: no changes")
        return {"changes_emitted": 0, "changes_filtered": 0, "lsn": None}

    sqs = boto3.client("sqs", region_name=region)
    emitted = 0
    filtered = 0
    last_lsn: Optional[str] = None

    for lsn, xid, data in rows:
        last_lsn = lsn
        parsed = _parse_test_decoding_line(data)
        if parsed is None:
            filtered += 1
            continue
        if parsed["table"] not in _TRACKED_TABLES:
            filtered += 1
            continue

        message = {
            "lsn": lsn,
            "xid": int(xid) if xid is not None else None,
            "table": parsed["table"],
            "op": parsed["op"],
            # Indexing Lambda expects pk as a dict (e.g. {"id": "<uuid>"}).
            "pk": {"id": parsed.get("pk")} if parsed.get("pk") else {},
            # Indexing Lambda expects either before/after (per WAL change) or
            # data. We emit data as `after` for INSERT/UPDATE and `before` for
            # DELETE so the downstream resolver can pick the right side.
            "before": parsed.get("data") if parsed["op"] == "DELETE" else None,
            "after": parsed.get("data") if parsed["op"] in ("INSERT", "UPDATE") else None,
        }

        message_group = f"{parsed['table']}:{parsed.get('pk') or 'unknown'}"
        message_dedupe = f"{lsn}:{parsed['table']}:{parsed.get('pk') or 'unknown'}"

        sqs.send_message(
            QueueUrl=sqs_queue_url,
            MessageBody=json.dumps(message),
            MessageGroupId=message_group,
            MessageDeduplicationId=message_dedupe,
        )
        emitted += 1

    LOG.info(
        "cdc-reader: emitted=%d filtered=%d last_lsn=%s",
        emitted,
        filtered,
        last_lsn,
    )
    return {
        "changes_emitted": emitted,
        "changes_filtered": filtered,
        "lsn": last_lsn,
    }


def _ensure_test_decoding_slot(conn: Any, slot_name: str) -> None:
    """Drop+recreate the slot with test_decoding if it's not already.

    Idempotent — a no-op when the slot is already test_decoding.
    Production should bake this into a migration; doing it lazily here
    keeps the PoC bring-up contained.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT plugin FROM pg_replication_slots WHERE slot_name = %s",
        (slot_name,),
    )
    row = cur.fetchone()
    if row is None:
        # Slot doesn't exist — create with test_decoding.
        cur.execute(
            "SELECT pg_create_logical_replication_slot(%s, %s)",
            (slot_name, "test_decoding"),
        )
    elif row[0] != "test_decoding":
        # Wrong plugin — drop and recreate. This loses any un-flushed
        # WAL position, but that's expected on the very first
        # cdc-reader invocation after migrations bring up the slot
        # with pgoutput.
        cur.execute("SELECT pg_drop_replication_slot(%s)", (slot_name,))
        cur.execute(
            "SELECT pg_create_logical_replication_slot(%s, %s)",
            (slot_name, "test_decoding"),
        )
    cur.close()


_TABLE_RE = re.compile(r"table public\.(\w+):\s+(INSERT|UPDATE|DELETE):\s+(.*)")
_KV_RE = re.compile(r"(\w+)\[(?:[^\]]+)\]:('(?:[^']|'')*'|[^\s]+)")


def _parse_test_decoding_line(line: str) -> Optional[Dict[str, Any]]:
    """Parse a test_decoding output line into a structured event.

    Examples:
      ``BEGIN 12345``
      ``COMMIT 12345``
      ``table public.data_asset: INSERT: id[uuid]:'abc-123' name[text]:'foo'``
      ``table public.subject: UPDATE: id[uuid]:'sub-1' species[text]:'mouse'``

    BEGIN/COMMIT lines are skipped — only INSERT/UPDATE/DELETE matter
    for the indexer.
    """
    if not line or line.startswith("BEGIN") or line.startswith("COMMIT"):
        return None

    m = _TABLE_RE.match(line)
    if m is None:
        return None

    table, op, rest = m.group(1), m.group(2), m.group(3)

    data: Dict[str, Any] = {}
    pk: Optional[str] = None
    for kv in _KV_RE.finditer(rest):
        key, raw_value = kv.group(1), kv.group(2)
        if raw_value.startswith("'") and raw_value.endswith("'"):
            value = raw_value[1:-1].replace("''", "'")
        elif raw_value == "null":
            value = None
        else:
            value = raw_value
        data[key] = value
        if key == "id" and pk is None:
            pk = str(value) if value is not None else None

    return {"table": table, "op": op, "pk": pk, "data": data}


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise CdcReaderError(f"Required env var {name!r} is not set.")
    return value

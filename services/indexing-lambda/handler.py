"""
Allen BioData Registry PoC — Indexing Lambda (CDC consumer).

Consumes WAL change events from the SQS FIFO queue provisioned by the
``cdc-pipeline`` Terraform module (Task 17.1), enriches each event by
JOINing back to Aurora to hydrate the denormalized
``space_id`` / ``org_id`` / ``is_sensitive`` fields plus related shared
entities, and fans out to two read stores:

* **DocumentDB** — MongoDB-shaped document, compatible with the
  existing ``aind-data-access-api`` consumers (R28.4).
* **OpenSearch** — flattened, denormalized document for single-query
  faceted + hybrid search. Each indexed document carries
  ``embedding_pending: true`` and ``description_vec: null``; the
  ``Embedding_Backfill_Lambda`` (Task 19.2) populates the vector
  asynchronously on a 30-second EventBridge schedule. **Indexing
  Lambda never calls Bedrock**; keeping embedding latency
  (500–1500 ms) out of the CDC critical path is a deliberate design
  decision — see design.md §Architecture.CDC Pipeline Architecture.

Independent fan-out
-------------------

A DocumentDB write failure does NOT block the OpenSearch write, and
vice versa. Each target gets its own try/except; failed events land
in the DLQ via ``sqs:SendMessage`` with ``target: "docdb" | "opensearch"``
so operators can replay only the failed leg.

Idempotency
-----------

Both writes are upsert-shaped (``replace_one(..., upsert=True)`` for
DocumentDB; ``index`` with explicit ``_id`` for OpenSearch). Re-delivery
of the same SQS message therefore produces the same end state.

Trust boundary
--------------

This Lambda runs as a privileged service-role identity that bypasses
RLS. It does NOT issue ``SET LOCAL app.current_user_id`` — the CDC
indexer needs to see every row regardless of governance scope so it
can produce visibility metadata for downstream queries. The dedicated
``cdc_indexer`` Postgres role is granted ``BYPASSRLS`` by the
migration runner; the IAM execution role is the only principal
permitted to assume that DB user (per ``rds-db:connect`` policy
scoping).

Validates: R1.7, R8.4, R17.9, R28.3, R28.4, R28.5, R28.6.

Design references:
  * design.md §Components.12. Indexing_Lambda.
  * design.md §Architecture.CDC Pipeline Architecture.
  * design.md §Data Models.DocumentDB Document Shape.
  * design.md §Data Models.OpenSearch Document Shape.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, MutableMapping, Optional, Tuple

# boto3 ships with the Lambda Python 3.12 runtime; the rest ride on
# this Lambda's own deployment image (see requirements.txt).
import boto3

# psycopg, pymongo, opensearch-py, and requests-aws4auth are bundled
# directly in the Lambda zip via requirements.txt (this Lambda does
# NOT consume the shared Layer for psycopg — the shared Layer's
# psycopg is configured for application-level RLS connections, while
# the indexer needs a privileged BYPASSRLS path).
#
# Imports are deferred into the per-client accessor functions because:
#   1. opensearch-py + requests-aws4auth pull in the `requests` library,
#      which probe-checks chardet/urllib3 versions at import time and
#      can emit warnings that some test environments escalate to
#      errors. Unit tests mock the clients, so the heavy deps should
#      never be imported during ``pytest``.
#   2. Cold-start cost is unaffected — each accessor is invoked exactly
#      once per Lambda execution environment, on the first warm call.


LOG = logging.getLogger()
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


# ---------------------------------------------------------------------------
# Constants — table allow-list & index naming.
# ---------------------------------------------------------------------------


# CDC events are filtered down to this allow-list before any work is done.
# Tables not in this set are skipped silently — there's no value in
# indexing app_user, entity_revision, lifecycle_transition, etc.
# (revision history is queried via the REST API, not the search layer).
_INDEXED_TABLES: frozenset[str] = frozenset(
    {
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
        "collection",
        # Junctions trigger a re-index of the parent data_asset. We
        # accept events on the junction tables so we can recompute the
        # denormalized arrays on the parent doc.
        "data_asset_subject",
        "data_asset_instrument",
    }
)


# Tables whose CDC events trigger a re-index of the asset they belong to,
# rather than producing a top-level document themselves.
_ASSET_CHILD_TABLES: frozenset[str] = frozenset(
    {
        "session",
        "acquisition",
        "processing",
        "quality_control",
        "data_description",
        "data_asset_subject",
        "data_asset_instrument",
    }
)


# Index / collection names. One per top-level entity type.
_DOCDB_DATABASE = "biodata_registry"
_DOCDB_COLLECTIONS: Mapping[str, str] = {
    "data_asset": "data_asset",
    "subject": "subject",
    "instrument": "instrument",
    "rig": "rig",
    "procedures": "procedures",
    "collection": "collection",
}

_OPENSEARCH_INDICES: Mapping[str, str] = {
    "data_asset": "data_asset",
    "subject": "subject",
    "instrument": "instrument",
}


# ---------------------------------------------------------------------------
# Module-level singletons (cold-start cache).
#
# These are populated lazily on the first invocation and reused across
# warm invocations. Lambda's execution-environment lifecycle keeps the
# module alive between calls, so connection setup is amortised.
# ---------------------------------------------------------------------------


_aurora_conn: Any = None
_docdb_client: Any = None
_opensearch_client: Any = None
_sqs_client: Any = None  # boto3 client, no static type
_secrets_cache: MutableMapping[str, Mapping[str, Any]] = {}


# ---------------------------------------------------------------------------
# Lambda entry point.
# ---------------------------------------------------------------------------


def lambda_handler(event: Mapping[str, Any], context: Any) -> Mapping[str, Any]:
    """SQS event handler.

    SQS Lambda integration delivers a batch of records in
    ``event["Records"]``. For each record:

      1. Parse the WAL event from the message body.
      2. Filter out tables we don't index.
      3. Hydrate via Aurora JOIN.
      4. Build DocumentDB + OpenSearch documents.
      5. Fan out to both stores independently. Failures land in the DLQ
         tagged by target.

    The handler does NOT raise on per-target failures — that would
    cause SQS to redeliver the entire batch and double-write the
    successful target. Instead, individual failures are routed to the
    DLQ with enough context for replay.

    Returns a summary dict for CloudWatch / X-Ray observability:
    ``{processed, skipped, docdb_failures, opensearch_failures}``.
    """
    records = list(event.get("Records") or [])
    summary: Dict[str, int] = {
        "processed": 0,
        "skipped": 0,
        "docdb_failures": 0,
        "opensearch_failures": 0,
    }

    if not records:
        LOG.info(json.dumps({"message": "empty SQS batch", "summary": summary}))
        return summary

    for record in records:
        message_id = record.get("messageId", "<unknown>")
        try:
            cdc_event = _parse_wal_event(record)
        except Exception as exc:  # noqa: BLE001 - log + skip
            LOG.exception(
                "failed to parse WAL event from SQS message %s: %s",
                message_id,
                exc,
            )
            # An unparseable message will be retried by SQS (visibility
            # timeout) and ultimately drop into the DLQ via the redrive
            # policy on the source queue. We don't manually requeue.
            summary["skipped"] += 1
            continue

        table = cdc_event.get("table")
        if table not in _INDEXED_TABLES:
            LOG.debug(
                json.dumps(
                    {
                        "message": "filtered table",
                        "event_id": message_id,
                        "table": table,
                    }
                )
            )
            summary["skipped"] += 1
            continue

        try:
            _process_event(cdc_event, message_id=message_id, summary=summary)
        except Exception as exc:  # noqa: BLE001 - we never want to raise
            # _process_event itself catches per-target failures. A bare
            # exception escaping here means hydration or routing failed
            # before either target was attempted — in that case route
            # the original event to the DLQ as ``target: "indexer"``.
            LOG.exception(
                "indexer-internal failure processing message %s: %s",
                message_id,
                exc,
            )
            _send_to_dlq(
                cdc_event,
                target="indexer",
                error=str(exc),
                message_id=message_id,
            )
            summary["skipped"] += 1

        summary["processed"] += 1

    LOG.info(json.dumps({"message": "batch complete", "summary": summary}))
    return summary


# ---------------------------------------------------------------------------
# WAL event parsing.
# ---------------------------------------------------------------------------


def _parse_wal_event(record: Mapping[str, Any]) -> Dict[str, Any]:
    """Parse the WAL change event from an SQS record.

    The cdc-pipeline module sends JSON message bodies (the CDC Reader
    Lambda formats events as ``{op, schema, table, ts_ms, lsn, before,
    after, pk}``). We accept either a JSON-string body or, defensively,
    an already-decoded dict (some EventBridge Pipes input transformer
    configurations decode the body upstream).
    """
    body = record.get("body")
    if isinstance(body, str):
        return json.loads(body)
    if isinstance(body, Mapping):
        return dict(body)
    raise ValueError(
        f"SQS record body is neither str nor mapping: type={type(body).__name__!r}"
    )


# ---------------------------------------------------------------------------
# Per-event processing.
# ---------------------------------------------------------------------------


def _process_event(
    cdc_event: Mapping[str, Any],
    *,
    message_id: str,
    summary: MutableMapping[str, int],
) -> None:
    """Hydrate the event and fan out to DocumentDB + OpenSearch.

    Independent fan-out: a DocumentDB write failure does NOT prevent
    the OpenSearch write from running. Each target gets its own
    try/except; failures are routed to the DLQ tagged by target.
    """
    op = cdc_event.get("op")
    table = cdc_event.get("table")
    pk = cdc_event.get("pk") or {}

    # Resolve the target table + entity id we should produce a doc for.
    # Asset-child events trigger a re-index of the parent data_asset.
    entity_table, entity_id = _resolve_index_target(cdc_event)
    if entity_table is None or entity_id is None:
        LOG.debug(
            json.dumps(
                {
                    "message": "no index target",
                    "event_id": message_id,
                    "op": op,
                    "table": table,
                }
            )
        )
        return

    log_base: Dict[str, Any] = {
        "event_id": message_id,
        "op": op,
        "table": table,
        "entity_table": entity_table,
        "entity_id": entity_id,
    }

    # DELETE: remove from both stores, no hydration needed.
    if op == "D":
        _safe_delete_docdb(entity_table, entity_id, log_base, summary)
        _safe_delete_opensearch(entity_table, entity_id, log_base, summary)
        return

    # INSERT / UPDATE: hydrate the row and shape both documents.
    try:
        hydrated = _hydrate(entity_table, entity_id)
    except Exception as exc:  # noqa: BLE001 - hydration failures are fatal for this event
        LOG.exception("hydration failure for %s/%s: %s", entity_table, entity_id, exc)
        _send_to_dlq(
            cdc_event,
            target="indexer",
            error=f"hydration_failed: {exc}",
            message_id=message_id,
        )
        return

    if hydrated is None:
        # The row was deleted between the WAL event and our hydration
        # query — treat as a delete to keep the read stores consistent.
        LOG.info(
            json.dumps(
                {
                    **log_base,
                    "message": "row missing at hydration time, treating as delete",
                }
            )
        )
        _safe_delete_docdb(entity_table, entity_id, log_base, summary)
        _safe_delete_opensearch(entity_table, entity_id, log_base, summary)
        return

    # Build target documents.
    docdb_doc = _build_docdb_document(entity_table, hydrated)
    opensearch_doc = _build_opensearch_document(entity_table, hydrated)

    # Fan out — independently. One failure does not block the other.
    _safe_upsert_docdb(
        entity_table, entity_id, docdb_doc, cdc_event, log_base, summary, message_id
    )
    _safe_index_opensearch(
        entity_table,
        entity_id,
        opensearch_doc,
        cdc_event,
        log_base,
        summary,
        message_id,
    )


def _resolve_index_target(
    cdc_event: Mapping[str, Any],
) -> Tuple[Optional[str], Optional[str]]:
    """Map a CDC event to the (top-level entity table, entity id) it indexes.

    Asset-child events (session, acquisition, processing, quality_control,
    data_description, junctions) trigger a re-index of the parent
    data_asset. Top-level entity events index themselves.
    """
    table = cdc_event.get("table")
    after = cdc_event.get("after") or {}
    before = cdc_event.get("before") or {}
    pk = cdc_event.get("pk") or {}

    if table not in _INDEXED_TABLES:
        return None, None

    if table in _ASSET_CHILD_TABLES:
        # Find the data_asset_id to re-index. Most asset-child rows
        # have it directly; junctions have it under the same name.
        asset_id = (
            after.get("data_asset_id")
            or before.get("data_asset_id")
            or pk.get("data_asset_id")
        )
        if not asset_id:
            return None, None
        return "data_asset", str(asset_id)

    # Top-level entity — id is in pk or after/before.
    entity_id = (
        pk.get("id")
        or after.get("id")
        or before.get("id")
    )
    if not entity_id:
        return None, None
    return table, str(entity_id)


# ---------------------------------------------------------------------------
# Aurora hydration.
# ---------------------------------------------------------------------------


def _hydrate(entity_table: str, entity_id: str) -> Optional[Mapping[str, Any]]:
    """Fetch the full denormalized row for an entity.

    For ``data_asset``: JOINs to ``space`` and ``organization`` to
    surface ``space_id`` / ``org_id`` / ``is_sensitive`` (driven by
    the asset's ``sensitive_flag``). Also pulls related shared and
    asset-specific entities so the downstream documents can embed
    them.

    For shared entities (subject, instrument, rig, procedures): a
    plain SELECT is enough — they don't carry per-asset state.

    For ``collection``: a plain SELECT.
    """
    conn = _get_aurora_connection()

    if entity_table == "data_asset":
        return _hydrate_data_asset(conn, entity_id)
    if entity_table in ("subject", "instrument", "rig", "procedures"):
        return _hydrate_shared_entity(conn, entity_table, entity_id)
    if entity_table == "collection":
        return _hydrate_collection(conn, entity_id)
    return None


def _hydrate_data_asset(
    conn: Any, asset_id: str
) -> Optional[Mapping[str, Any]]:
    """JOIN data_asset → space → organization and pull related entities.

    Returns ``None`` if the asset row no longer exists.
    """
    base_sql = """
        SELECT
            da.id,
            da.space_id,
            s.org_id AS org_id,
            o.name AS organization_name,
            da.name,
            da.display_name,
            da.storage_uri,
            da.data_type,
            da.lifecycle_state,
            da.validation_status,
            da.validation_errors,
            da.sensitive_flag,
            da.sensitive_flag_meta,
            da.schema_id,
            da.schema_version,
            da.provenance_source_id,
            da.description,
            da.metadata,
            da.created_by,
            da.created_at,
            da.updated_at,
            da.version
        FROM data_asset da
        JOIN space s ON s.id = da.space_id
        JOIN organization o ON o.id = s.org_id
        WHERE da.id = %s
    """
    with conn.cursor() as cur:
        cur.execute(base_sql, (asset_id,))
        row = _fetchone_dict(cur)
    if row is None:
        return None

    # Linked subjects via data_asset_subject junction.
    row["subjects"] = _fetch_linked_entities(
        conn,
        junction_table="data_asset_subject",
        target_table="subject",
        asset_id=asset_id,
    )

    # Linked instruments via data_asset_instrument junction.
    row["instruments"] = _fetch_linked_entities(
        conn,
        junction_table="data_asset_instrument",
        target_table="instrument",
        asset_id=asset_id,
    )

    # Asset-specific entities (1:N or 1:1 keyed on data_asset_id).
    row["sessions"] = _fetch_asset_children(conn, "session", asset_id)
    row["acquisitions"] = _fetch_asset_children(conn, "acquisition", asset_id)
    row["processings"] = _fetch_asset_children(conn, "processing", asset_id)
    row["quality_controls"] = _fetch_asset_children(
        conn, "quality_control", asset_id
    )
    row["data_descriptions"] = _fetch_asset_children(
        conn, "data_description", asset_id
    )

    return row


def _fetch_linked_entities(
    conn: Any,
    *,
    junction_table: str,
    target_table: str,
    asset_id: str,
) -> List[Mapping[str, Any]]:
    """Fetch entities linked to an asset via a junction table."""
    sql = f"""
        SELECT t.*
        FROM {target_table} t
        JOIN {junction_table} j ON j.{target_table}_id = t.id
        WHERE j.data_asset_id = %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (asset_id,))
        return _fetchall_dicts(cur)


def _fetch_asset_children(
    conn: Any,
    table: str,
    asset_id: str,
) -> List[Mapping[str, Any]]:
    """Fetch asset-specific entity rows keyed on data_asset_id."""
    sql = f"SELECT * FROM {table} WHERE data_asset_id = %s"
    with conn.cursor() as cur:
        cur.execute(sql, (asset_id,))
        return _fetchall_dicts(cur)


def _hydrate_shared_entity(
    conn: Any,
    table: str,
    entity_id: str,
) -> Optional[Mapping[str, Any]]:
    """Fetch a shared entity row by id."""
    sql = f"SELECT * FROM {table} WHERE id = %s"
    with conn.cursor() as cur:
        cur.execute(sql, (entity_id,))
        return _fetchone_dict(cur)


def _hydrate_collection(
    conn: Any, collection_id: str
) -> Optional[Mapping[str, Any]]:
    """Fetch a collection row joined to its organization."""
    sql = """
        SELECT c.*, o.name AS organization_name
        FROM collection c
        LEFT JOIN organization o ON o.id = c.org_id
        WHERE c.id = %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (collection_id,))
        return _fetchone_dict(cur)


def _fetchone_dict(cur: Any) -> Optional[Dict[str, Any]]:
    """Convert a psycopg cursor row into a dict by column name."""
    row = cur.fetchone()
    if row is None:
        return None
    if cur.description is None:
        return None
    columns = [d[0] for d in cur.description]
    return dict(zip(columns, row))


def _fetchall_dicts(cur: Any) -> List[Dict[str, Any]]:
    """Convert all psycopg cursor rows into dicts."""
    rows = cur.fetchall()
    if not rows:
        return []
    if cur.description is None:
        return []
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row)) for row in rows]


# ---------------------------------------------------------------------------
# Document shaping — DocumentDB.
# ---------------------------------------------------------------------------


def _build_docdb_document(
    entity_table: str, hydrated: Mapping[str, Any]
) -> Dict[str, Any]:
    """Shape the hydrated row into the MongoDB-compatible document.

    Schema follows design.md §Data Models.DocumentDB Document Shape so
    the existing aind-data-access-api consumers keep working.
    """
    if entity_table == "data_asset":
        return _build_docdb_data_asset(hydrated)
    if entity_table in ("subject", "instrument", "rig", "procedures"):
        return _build_docdb_shared_entity(hydrated)
    if entity_table == "collection":
        return _build_docdb_collection(hydrated)
    # Unreachable — guarded by _INDEXED_TABLES at the entry point.
    return {"_id": str(hydrated.get("id"))}


def _build_docdb_data_asset(row: Mapping[str, Any]) -> Dict[str, Any]:
    """MongoDB-shaped data_asset document."""
    subjects = row.get("subjects") or []
    instruments = row.get("instruments") or []
    sessions = row.get("sessions") or []
    acquisitions = row.get("acquisitions") or []
    processings = row.get("processings") or []
    quality_controls = row.get("quality_controls") or []
    data_descriptions = row.get("data_descriptions") or []

    # Pick the canonical "first" entry for shape compatibility with
    # the existing single-entity nested fields in metadata_v2.
    primary_subject = subjects[0] if subjects else None
    primary_instrument = instruments[0] if instruments else None
    primary_session = sessions[0] if sessions else None
    primary_acquisition = acquisitions[0] if acquisitions else None
    primary_data_description = data_descriptions[0] if data_descriptions else None

    return {
        "_id": str(row["id"]),
        "name": row.get("name"),
        "display_name": row.get("display_name"),
        "storage_uri": row.get("storage_uri"),
        "data_type": row.get("data_type"),
        "lifecycle_state": row.get("lifecycle_state"),
        "validation_status": row.get("validation_status"),
        "space_id": str(row["space_id"]) if row.get("space_id") else None,
        "org_id": str(row["org_id"]) if row.get("org_id") else None,
        "organization_name": row.get("organization_name"),
        "sensitive_flag": bool(row.get("sensitive_flag")),
        "is_sensitive": bool(row.get("sensitive_flag")),
        "schema_id": str(row["schema_id"]) if row.get("schema_id") else None,
        "schema_version": row.get("schema_version"),
        "provenance_source_id": (
            str(row["provenance_source_id"])
            if row.get("provenance_source_id")
            else None
        ),
        "description": row.get("description"),
        # Nested entity blocks for back-compat with aind-data-access-api.
        "subject": _strip_internal(primary_subject),
        "subjects": [_strip_internal(s) for s in subjects],
        "instrument": _strip_internal(primary_instrument),
        "instruments": [_strip_internal(i) for i in instruments],
        "session": _strip_internal(primary_session),
        "sessions": [_strip_internal(s) for s in sessions],
        "acquisition": _strip_internal(primary_acquisition),
        "acquisitions": [_strip_internal(a) for a in acquisitions],
        "processing": [_strip_internal(p) for p in processings],
        "quality_control": [_strip_internal(q) for q in quality_controls],
        "data_description": _strip_internal(primary_data_description),
        "metadata": row.get("metadata") or {},
        "_revision": row.get("version"),
        "_updated_at": _isoformat(row.get("updated_at")),
        "_created_at": _isoformat(row.get("created_at")),
    }


def _build_docdb_shared_entity(row: Mapping[str, Any]) -> Dict[str, Any]:
    """MongoDB-shaped shared entity document (subject / instrument / rig / procedures)."""
    doc: Dict[str, Any] = dict(_strip_internal(row) or {})
    doc["_id"] = str(row["id"])
    doc.pop("id", None)
    doc["_updated_at"] = _isoformat(row.get("updated_at"))
    doc["_created_at"] = _isoformat(row.get("created_at"))
    return doc


def _build_docdb_collection(row: Mapping[str, Any]) -> Dict[str, Any]:
    """MongoDB-shaped collection document."""
    doc: Dict[str, Any] = dict(_strip_internal(row) or {})
    doc["_id"] = str(row["id"])
    doc.pop("id", None)
    doc["org_id"] = str(row["org_id"]) if row.get("org_id") else None
    doc["_updated_at"] = _isoformat(row.get("updated_at"))
    doc["_created_at"] = _isoformat(row.get("created_at"))
    return doc


def _strip_internal(row: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """Stringify UUIDs / datetimes so the document is JSON-safe."""
    if row is None:
        return None
    out: Dict[str, Any] = {}
    for key, value in row.items():
        out[key] = _to_jsonable(value)
    return out


# ---------------------------------------------------------------------------
# Document shaping — OpenSearch.
# ---------------------------------------------------------------------------


def _build_opensearch_document(
    entity_table: str, hydrated: Mapping[str, Any]
) -> Dict[str, Any]:
    """Shape the hydrated row into the flattened OpenSearch document.

    The document carries ``embedding_pending: true`` and
    ``description_vec: null`` — the Embedding_Backfill_Lambda fills
    these in asynchronously. Indexing_Lambda NEVER calls Bedrock; the
    embedding-backfill split is the design's explicit way to keep the
    CDC critical path free of 500–1500 ms embedding latency.
    """
    if entity_table == "data_asset":
        doc = _build_opensearch_data_asset(hydrated)
    elif entity_table in ("subject", "instrument"):
        doc = _build_opensearch_shared_entity(entity_table, hydrated)
    else:
        # Other entity types don't have a dedicated index in the PoC.
        doc = _build_opensearch_shared_entity(entity_table, hydrated)

    # Async embedding contract — these two fields are the marker for
    # Embedding_Backfill_Lambda's pending-document scan. Keep them as
    # the LAST step so they overwrite anything an earlier shaper might
    # have set.
    doc["embedding_pending"] = True
    doc["description_vec"] = None
    return doc


def _build_opensearch_data_asset(row: Mapping[str, Any]) -> Dict[str, Any]:
    """Flattened, denormalized data_asset document for OpenSearch."""
    subjects = row.get("subjects") or []
    instruments = row.get("instruments") or []
    sessions = row.get("sessions") or []
    primary_subject = subjects[0] if subjects else {}
    primary_instrument = instruments[0] if instruments else {}

    # Aggregate modalities across linked rigs / instruments / sessions.
    modalities: List[str] = []
    for instr in instruments:
        if isinstance(instr.get("modalities"), list):
            modalities.extend(str(m) for m in instr["modalities"])
    for sess in sessions:
        if isinstance(sess.get("modalities"), list):
            modalities.extend(str(m) for m in sess["modalities"])

    name = row.get("name") or row.get("display_name") or ""
    description = row.get("description") or ""

    return {
        "id": str(row["id"]),
        "space_id": str(row["space_id"]) if row.get("space_id") else None,
        "org_id": str(row["org_id"]) if row.get("org_id") else None,
        "is_sensitive": bool(row.get("sensitive_flag")),
        "lifecycle_state": row.get("lifecycle_state"),
        "validation_status": row.get("validation_status"),
        "storage_uri": row.get("storage_uri"),
        "name": name,
        "name_suggest": name,
        "display_name": row.get("display_name"),
        # Denormalized fields for facet pushdown without joins.
        "species": primary_subject.get("species") if primary_subject else None,
        "sex": primary_subject.get("sex") if primary_subject else None,
        "subject_id": (
            primary_subject.get("subject_id") if primary_subject else None
        ),
        "instrument_name": (
            primary_instrument.get("model")
            or primary_instrument.get("instrument_id")
            if primary_instrument
            else None
        ),
        "organization": row.get("organization_name"),
        "modalities": sorted(set(modalities)) if modalities else [],
        "description": description,
        "metadata_flat": _flatten_metadata(row.get("metadata") or {}),
        "data_type": row.get("data_type"),
        "schema_id": str(row["schema_id"]) if row.get("schema_id") else None,
        "schema_version": row.get("schema_version"),
        "created_at": _isoformat(row.get("created_at")),
        "updated_at": _isoformat(row.get("updated_at")),
    }


def _build_opensearch_shared_entity(
    entity_table: str, row: Mapping[str, Any]
) -> Dict[str, Any]:
    """Flattened OpenSearch document for shared entities."""
    base: Dict[str, Any] = {
        "id": str(row["id"]),
        "name": row.get("name") or row.get("display_name") or row.get(f"{entity_table}_id"),
        "name_suggest": (
            row.get("name") or row.get("display_name") or row.get(f"{entity_table}_id")
        ),
        "description": row.get("notes") or row.get("description") or "",
        "created_at": _isoformat(row.get("created_at")),
        "updated_at": _isoformat(row.get("updated_at")),
    }
    if entity_table == "subject":
        base.update(
            {
                "subject_id": row.get("subject_id"),
                "species": row.get("species"),
                "sex": row.get("sex"),
                "genotype": row.get("genotype"),
            }
        )
    elif entity_table == "instrument":
        base.update(
            {
                "instrument_id": row.get("instrument_id"),
                "instrument_type": row.get("instrument_type"),
                "manufacturer": row.get("manufacturer"),
                "model": row.get("model"),
            }
        )
    return base


def _flatten_metadata(metadata: Mapping[str, Any]) -> str:
    """Flatten a JSONB metadata blob into a searchable string.

    Joining the values with newlines gives OpenSearch's text analyzer a
    single field to BM25 against, without losing any of the JSONB
    content. Keys are skipped — search is over values, not field names.
    """
    parts: List[str] = []
    _flatten_into(metadata, parts)
    return "\n".join(parts)


def _flatten_into(value: Any, sink: List[str]) -> None:
    if isinstance(value, Mapping):
        for v in value.values():
            _flatten_into(v, sink)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _flatten_into(v, sink)
    elif value is None:
        return
    else:
        sink.append(str(value))


# ---------------------------------------------------------------------------
# Independent fan-out — DocumentDB.
# ---------------------------------------------------------------------------


def _safe_upsert_docdb(
    entity_table: str,
    entity_id: str,
    doc: Mapping[str, Any],
    cdc_event: Mapping[str, Any],
    log_base: Mapping[str, Any],
    summary: MutableMapping[str, int],
    message_id: str,
) -> None:
    """Upsert into DocumentDB. On failure, route the event to the DLQ."""
    collection_name = _DOCDB_COLLECTIONS.get(entity_table)
    if collection_name is None:
        # No DocumentDB target for this entity type (e.g. shared entity
        # types we only index in OpenSearch). Skip silently.
        return

    try:
        client = _get_docdb_client()
        db = client[_DOCDB_DATABASE]
        db[collection_name].replace_one({"_id": entity_id}, dict(doc), upsert=True)
        LOG.info(
            json.dumps(
                {**log_base, "target": "docdb", "outcome": "ok"},
                default=str,
            )
        )
    except Exception as exc:  # noqa: BLE001 - independent fan-out
        LOG.exception(
            "DocumentDB upsert failed for %s/%s: %s",
            entity_table,
            entity_id,
            exc,
        )
        summary["docdb_failures"] += 1
        _send_to_dlq(
            cdc_event,
            target="docdb",
            error=str(exc),
            message_id=message_id,
        )


def _safe_delete_docdb(
    entity_table: str,
    entity_id: str,
    log_base: Mapping[str, Any],
    summary: MutableMapping[str, int],
) -> None:
    """Delete from DocumentDB. Failures only logged — DELETE has no DLQ."""
    collection_name = _DOCDB_COLLECTIONS.get(entity_table)
    if collection_name is None:
        return
    try:
        client = _get_docdb_client()
        db = client[_DOCDB_DATABASE]
        db[collection_name].delete_one({"_id": entity_id})
        LOG.info(
            json.dumps(
                {**log_base, "target": "docdb", "outcome": "deleted"},
                default=str,
            )
        )
    except Exception as exc:  # noqa: BLE001
        LOG.exception(
            "DocumentDB delete failed for %s/%s: %s",
            entity_table,
            entity_id,
            exc,
        )
        summary["docdb_failures"] += 1


# ---------------------------------------------------------------------------
# Independent fan-out — OpenSearch.
# ---------------------------------------------------------------------------


def _safe_index_opensearch(
    entity_table: str,
    entity_id: str,
    doc: Mapping[str, Any],
    cdc_event: Mapping[str, Any],
    log_base: Mapping[str, Any],
    summary: MutableMapping[str, int],
    message_id: str,
) -> None:
    """Index into OpenSearch. On failure, route the event to the DLQ."""
    index_name = _OPENSEARCH_INDICES.get(entity_table)
    if index_name is None:
        # Not all entity types have OpenSearch indices in the PoC.
        return

    try:
        client = _get_opensearch_client()
        client.index(index=index_name, id=entity_id, body=dict(doc))
        LOG.info(
            json.dumps(
                {**log_base, "target": "opensearch", "outcome": "ok"},
                default=str,
            )
        )
    except Exception as exc:  # noqa: BLE001 - independent fan-out
        LOG.exception(
            "OpenSearch index failed for %s/%s: %s",
            entity_table,
            entity_id,
            exc,
        )
        summary["opensearch_failures"] += 1
        _send_to_dlq(
            cdc_event,
            target="opensearch",
            error=str(exc),
            message_id=message_id,
        )


def _safe_delete_opensearch(
    entity_table: str,
    entity_id: str,
    log_base: Mapping[str, Any],
    summary: MutableMapping[str, int],
) -> None:
    """Delete from OpenSearch. Treat 404 as success (idempotency)."""
    index_name = _OPENSEARCH_INDICES.get(entity_table)
    if index_name is None:
        return
    try:
        client = _get_opensearch_client()
        client.delete(index=index_name, id=entity_id, ignore=[404])
        LOG.info(
            json.dumps(
                {**log_base, "target": "opensearch", "outcome": "deleted"},
                default=str,
            )
        )
    except Exception as exc:  # noqa: BLE001
        LOG.exception(
            "OpenSearch delete failed for %s/%s: %s",
            entity_table,
            entity_id,
            exc,
        )
        summary["opensearch_failures"] += 1


# ---------------------------------------------------------------------------
# DLQ.
# ---------------------------------------------------------------------------


def _send_to_dlq(
    cdc_event: Mapping[str, Any],
    *,
    target: str,
    error: str,
    message_id: str,
) -> None:
    """Send the failed event to the DLQ tagged by target.

    ``target`` is one of ``"docdb"``, ``"opensearch"``, or ``"indexer"``
    so operators can replay only the failed leg.
    """
    dlq_url = os.environ.get("DLQ_URL")
    if not dlq_url:
        LOG.error(
            "DLQ_URL is not set; dropping failed event %s target=%s",
            message_id,
            target,
        )
        return

    sqs = _get_sqs_client()
    payload = {
        "target": target,
        "error": error,
        "message_id": message_id,
        "cdc_event": dict(cdc_event),
        "ts_ms": int(time.time() * 1000),
    }

    # The DLQ provisioned by cdc-pipeline is FIFO; FIFO queues require
    # MessageGroupId. Group by the failing target so DLQ readers can
    # subscribe to a single leg.
    try:
        sqs.send_message(
            QueueUrl=dlq_url,
            MessageBody=json.dumps(payload, default=str),
            MessageGroupId=f"indexing-{target}",
            MessageDeduplicationId=f"{message_id}:{target}",
        )
    except Exception as exc:  # noqa: BLE001 - DLQ failure is best-effort
        LOG.exception(
            "failed to enqueue DLQ message for %s target=%s: %s",
            message_id,
            target,
            exc,
        )


# ---------------------------------------------------------------------------
# Singleton accessors.
# ---------------------------------------------------------------------------


def _get_aurora_connection():
    """Open (or return cached) Aurora connection.

    The indexer connects as a privileged role with ``BYPASSRLS`` so it
    can see every row regardless of governance scope (this is the
    deliberate trust-boundary design — see module docstring). The
    connection does NOT issue ``SET LOCAL app.current_user_id``.
    """
    global _aurora_conn  # noqa: PLW0603
    if _aurora_conn is not None and not _aurora_conn.closed:
        return _aurora_conn

    # Lazy import — see the import-strategy comment at the top of the
    # module. Unit tests monkey-patch this accessor so the import
    # never fires during pytest.
    import psycopg  # type: ignore[import-untyped]

    secret_arn = os.environ["AURORA_SECRET_ARN"]
    secret = _get_secret(secret_arn)
    host = os.environ["AURORA_HOST"]
    port = int(os.environ.get("AURORA_PORT", "5432"))
    db = os.environ.get("AURORA_DB", secret.get("dbname", "biodata_registry"))
    user = secret.get("username", "cdc_indexer")
    password = secret["password"]

    _aurora_conn = psycopg.connect(
        host=host,
        port=port,
        dbname=db,
        user=user,
        password=password,
        sslmode=os.environ.get("DB_SSLMODE", "require"),
        connect_timeout=int(os.environ.get("DB_CONNECT_TIMEOUT_SECONDS", "10")),
        autocommit=True,
    )
    return _aurora_conn


def _get_docdb_client():
    """Open (or return cached) DocumentDB client.

    The indexer authenticates with the cluster master credentials
    fetched from Secrets Manager. IAM auth is reserved for external
    aind-data-access-api consumers — the indexer is a service-to-
    service path inside the VPC.
    """
    global _docdb_client  # noqa: PLW0603
    if _docdb_client is not None:
        return _docdb_client

    # Lazy import — see top-of-module comment.
    from pymongo import MongoClient  # type: ignore[import-untyped]

    secret_arn = os.environ["DOCDB_SECRET_ARN"]
    secret = _get_secret(secret_arn)
    endpoint = os.environ["DOCDB_ENDPOINT"]
    port = int(os.environ.get("DOCDB_PORT", "27017"))
    user = secret["username"]
    password = secret["password"]

    # DocumentDB requires TLS. The official AWS RDS root CA bundle is
    # bundled into the Lambda Layer at /opt/certs/global-bundle.pem
    # in production; locally we fall back to the system cert store.
    ca_bundle = os.environ.get("DOCDB_CA_BUNDLE", "/opt/certs/global-bundle.pem")
    tls_kwargs: Dict[str, Any] = {"tls": True, "retryWrites": False}
    if os.path.exists(ca_bundle):
        tls_kwargs["tlsCAFile"] = ca_bundle

    _docdb_client = MongoClient(
        host=endpoint,
        port=port,
        username=user,
        password=password,
        **tls_kwargs,
    )
    return _docdb_client


def _get_opensearch_client():
    """Open (or return cached) OpenSearch client signing with SigV4."""
    global _opensearch_client  # noqa: PLW0603
    if _opensearch_client is not None:
        return _opensearch_client

    # Lazy imports — opensearch-py + requests-aws4auth pull in
    # `requests`, which we want to avoid touching during unit tests.
    from opensearchpy import (  # type: ignore[import-untyped]
        OpenSearch,
        RequestsHttpConnection,
    )
    from requests_aws4auth import AWS4Auth  # type: ignore[import-untyped]

    endpoint = os.environ["OPENSEARCH_ENDPOINT"]
    region = os.environ.get(
        "OPENSEARCH_REGION",
        os.environ.get("AWS_REGION", "us-west-2"),
    )

    creds = boto3.Session().get_credentials()
    if creds is None:  # pragma: no cover
        raise RuntimeError(
            "no AWS credentials available for OpenSearch SigV4 signing"
        )
    awsauth = AWS4Auth(
        creds.access_key,
        creds.secret_key,
        region,
        "aoss",
        session_token=creds.token,
    )

    # Strip protocol — opensearch-py wants host only with use_ssl.
    host = endpoint.replace("https://", "").replace("http://", "")
    _opensearch_client = OpenSearch(
        hosts=[{"host": host, "port": 443}],
        http_auth=awsauth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        timeout=30,
    )
    return _opensearch_client


def _get_sqs_client():
    """Open (or return cached) SQS client."""
    global _sqs_client  # noqa: PLW0603
    if _sqs_client is None:
        _sqs_client = boto3.client(
            "sqs",
            region_name=os.environ.get("AWS_REGION", "us-west-2"),
        )
    return _sqs_client


def _get_secret(secret_arn: str) -> Mapping[str, Any]:
    """Fetch + cache a Secrets Manager secret, parsing its SecretString as JSON."""
    cached = _secrets_cache.get(secret_arn)
    if cached is not None:
        return cached
    sm = boto3.client(
        "secretsmanager",
        region_name=os.environ.get("AWS_REGION", "us-west-2"),
    )
    resp = sm.get_secret_value(SecretId=secret_arn)
    raw = resp.get("SecretString") or "{}"
    parsed = json.loads(raw)
    _secrets_cache[secret_arn] = parsed
    return parsed


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _to_jsonable(value: Any) -> Any:
    """Convert non-JSON-native types (UUID, datetime, Decimal) to safe forms."""
    if isinstance(value, datetime):
        return _isoformat(value)
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, Mapping):
        return {k: _to_jsonable(v) for k, v in value.items()}
    # Fall through: psycopg returns most types as native Python; UUID,
    # Decimal, etc. stringify cleanly. We don't try to be clever about
    # types we don't know about.
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _isoformat(value: Any) -> Optional[str]:
    """Normalise a datetime / string to ISO-8601 UTC, or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)

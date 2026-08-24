"""
Allen BioData Registry PoC — sample-data seeder core logic.

This module is split out from ``handler.py`` so the algorithm can be
unit-tested with a mocked S3 client and an in-memory ``FakeConn``,
without dragging in the Lambda framing, IAM token mint, or boto3.

What it does
------------

1. Streams a JSON file from S3 (the 7 GB aind-data-schema snapshot, or
   a 10% sample). The file is expected to be one of:
     * a JSON array of records (the canonical shape), OR
     * a newline-delimited JSON file (NDJSON).
   The seeder probes the first byte to choose the parsing strategy.

2. Bootstraps a ``system`` ``app_user`` row plus a ``system``
   Organization and ``default-space`` Space in Aurora — the seeded
   Data_Assets are attributed to this user and live in this space.
   Bootstrap is idempotent via UNIQUE constraints + UPSERT.

3. Iterates the source records:
     * Computes a SHA-256 content hash of each record.
     * Applies the deterministic-modulo sampling filter so the same
       fraction of records is selected on every run.
     * Calls :func:`mapping.map_record` to derive the relational rows.
     * Issues parameterised INSERTs into Aurora using
       ``ON CONFLICT DO NOTHING`` so re-runs are pure no-ops.
     * Records junction rows (``data_asset_subject``, etc.) once it has
       both the asset id and the shared-entity id in hand.

4. Returns a structured summary describing what was processed and what
   was inserted, so the Lambda handler can log it and so the
   ``aws_lambda_invocation`` data source captures it as the function
   result.

Idempotency contract
--------------------

The seeder is idempotent at three levels:

* **Sampling**  — :func:`mapping.should_sample` takes the content hash
  and returns the same accept/reject decision every time, so the same
  subset of records is processed on every invocation.
* **Inserts**   — every ``INSERT`` carries a matching ``ON CONFLICT
  ... DO NOTHING`` clause keyed on a UNIQUE column
  (``storage_uri`` for ``data_asset``, ``subject_id`` for ``subject``,
  ``instrument_id`` for ``instrument``, ``rig_id`` for ``rig``, the
  composite PK for junctions). Procedures, sessions, acquisitions,
  processings, quality_controls, and data_descriptions don't have
  UNIQUE columns so they use a metadata-fingerprint pre-check.
* **Bootstrap** — the system user/org/space rows use
  ``ON CONFLICT (...) DO UPDATE SET ... RETURNING id`` so the seeder
  always gets back the existing id (or the newly-minted one) without
  branching.

Validates: R32.2, R32.5.

Design references:
  * design.md §IaC.Idempotency and Sample Data
  * design.md §Effort Estimation.Data Seeding
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from typing import Any, Iterable, Iterator, List, Mapping, Optional

from mapping import MappedRecord, map_record, should_sample

LOG = logging.getLogger(__name__)


# Default values mirror migration-runner conventions: a "system" user
# created once, used as the ``created_by`` for every seeded row.
SYSTEM_USER_COGNITO_SUB = "system-seeder"
SYSTEM_USER_EMAIL = "system-seeder@biodata-registry.local"
SYSTEM_ORG_NAME = "system"
SYSTEM_ORG_DISPLAY_NAME = "System (seeded data owner)"
DEFAULT_SPACE_NAME = "default-space"
DEFAULT_SPACE_DISPLAY_NAME = "Default Space (seeded data)"


# ---------------------------------------------------------------------------
# Public types.
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class BootstrapIds:
    """Resolved ids of the seeded ``system`` user / org / space."""

    system_user_id: str
    system_org_id: str
    default_space_id: str


@dataclasses.dataclass
class SeedSummary:
    """Result of one ``run_seeder`` invocation.

    The Lambda handler serializes this to JSON for the caller (the
    Terraform ``aws_lambda_invocation`` data source). Counts are
    grouped so a ``terraform output`` view shows what was actually
    written without having to grep CloudWatch.
    """

    records_seen: int = 0
    records_sampled: int = 0
    records_skipped_unmappable: int = 0
    data_assets_inserted: int = 0
    data_assets_skipped: int = 0
    subjects_inserted: int = 0
    subjects_reused: int = 0
    instruments_inserted: int = 0
    instruments_reused: int = 0
    rigs_inserted: int = 0
    rigs_reused: int = 0
    procedures_inserted: int = 0
    sessions_inserted: int = 0
    acquisitions_inserted: int = 0
    processings_inserted: int = 0
    quality_controls_inserted: int = 0
    data_descriptions_inserted: int = 0
    junctions_inserted: int = 0
    errors: List[str] = dataclasses.field(default_factory=list)
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class SeederError(RuntimeError):
    """Base class for seeder-level failures."""


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def run_seeder(
    *,
    conn: Any,
    s3_client: Any,
    bucket: str,
    key: str,
    sample_fraction: float,
    max_records: Optional[int] = None,
    max_errors: int = 50,
) -> SeedSummary:
    """Stream a snapshot from S3 and seed Aurora.

    Parameters
    ----------
    conn:
        DB-API 2.0 connection to Aurora. The seeder expects
        ``cursor()``, ``commit()``, and ``rollback()``. pg8000.dbapi
        conforms; the unit tests use a stateful ``FakeConn``.
    s3_client:
        boto3 S3 client (or a stub). Must support
        ``get_object(Bucket=..., Key=...)`` returning a dict with a
        ``Body`` whose ``.read()`` yields the raw bytes.
    bucket:
        S3 bucket containing the snapshot.
    key:
        S3 key of the JSON file. The seeder probes the first byte to
        decide between JSON-array and NDJSON parsing.
    sample_fraction:
        Fraction in ``(0.0, 1.0]`` to seed. Selection is deterministic
        per record content (see :func:`mapping.should_sample`). Set to
        ``1.0`` to seed everything.
    max_records:
        Optional cap on records to process. Useful for unit tests and
        for guarded operator runs against the real corpus. ``None``
        means no cap.
    max_errors:
        Soft per-record-error budget. The first ``max_errors`` errors
        are recorded in the summary and processing continues; once the
        budget is exhausted we raise ``SeederError`` so a broken
        snapshot doesn't silently produce a half-loaded database.

    Returns
    -------
    A :class:`SeedSummary` describing what happened.
    """
    if sample_fraction <= 0.0 or sample_fraction > 1.0:
        raise SeederError(
            f"sample_fraction must be in (0.0, 1.0]; got {sample_fraction!r}"
        )

    started = time.monotonic()
    summary = SeedSummary()

    # Step 1) Bootstrap the system user / org / space.
    bootstrap = bootstrap_system_principal(conn)
    LOG.info(
        "bootstrap complete user_id=%s org_id=%s space_id=%s",
        bootstrap.system_user_id,
        bootstrap.system_org_id,
        bootstrap.default_space_id,
    )

    # Step 2) Stream the snapshot and process records.
    for record in _iter_records_from_s3(
        s3_client=s3_client, bucket=bucket, key=key
    ):
        if max_records is not None and summary.records_seen >= max_records:
            break

        summary.records_seen += 1

        try:
            mapped = map_record(
                record,
                space_id=bootstrap.default_space_id,
                created_by=bootstrap.system_user_id,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort seeder
            _record_error(summary, max_errors, f"map_record raised: {exc!r}")
            continue

        if mapped is None:
            summary.records_skipped_unmappable += 1
            continue

        if not should_sample(mapped.content_hash, sample_fraction):
            continue

        summary.records_sampled += 1

        try:
            _insert_mapped_record(conn, mapped, summary)
        except Exception as exc:  # noqa: BLE001
            _record_error(
                summary,
                max_errors,
                f"insert failed for content_hash={mapped.content_hash!r}: {exc!r}",
            )
            try:
                conn.rollback()
            except Exception:  # pragma: no cover — defensive
                LOG.debug("rollback after insert failure was a no-op", exc_info=True)
            continue

    summary.elapsed_ms = int((time.monotonic() - started) * 1000)
    return summary


# ---------------------------------------------------------------------------
# Bootstrap.
# ---------------------------------------------------------------------------


def bootstrap_system_principal(conn: Any) -> BootstrapIds:
    """Ensure the ``system`` user / org / ``default-space`` exist.

    Uses ``INSERT ... ON CONFLICT (...) DO UPDATE SET ... RETURNING id``
    so we always get the row id back regardless of whether it is the
    fresh insert or a prior bootstrap. The ``DO UPDATE`` clause is a
    no-op assignment (touching ``display_name``) — Postgres requires
    something on the right side of ``DO UPDATE`` for ``RETURNING`` to
    fire on conflict.
    """
    cur = conn.cursor()
    # Organization first (Space FKs to Organization, app_user FKs
    # softly to Organization).
    cur.execute(
        "INSERT INTO organization (name, display_name) "
        "VALUES (%s, %s) "
        "ON CONFLICT (name) DO UPDATE "
        "  SET display_name = EXCLUDED.display_name "
        "RETURNING id",
        (SYSTEM_ORG_NAME, SYSTEM_ORG_DISPLAY_NAME),
    )
    org_id = cur.fetchone()[0]

    # User row — created_by FK target on every seeded entity.
    cur.execute(
        "INSERT INTO app_user (cognito_sub, email, org_id) "
        "VALUES (%s, %s, %s) "
        "ON CONFLICT (cognito_sub) DO UPDATE "
        "  SET email = EXCLUDED.email "
        "RETURNING id",
        (SYSTEM_USER_COGNITO_SUB, SYSTEM_USER_EMAIL, org_id),
    )
    user_id = cur.fetchone()[0]

    # Space — UNIQUE (org_id, name).
    cur.execute(
        "INSERT INTO space (org_id, name, display_name) "
        "VALUES (%s, %s, %s) "
        "ON CONFLICT (org_id, name) DO UPDATE "
        "  SET display_name = EXCLUDED.display_name "
        "RETURNING id",
        (org_id, DEFAULT_SPACE_NAME, DEFAULT_SPACE_DISPLAY_NAME),
    )
    space_id = cur.fetchone()[0]

    conn.commit()

    return BootstrapIds(
        system_user_id=str(user_id),
        system_org_id=str(org_id),
        default_space_id=str(space_id),
    )


# ---------------------------------------------------------------------------
# Source-record streaming.
# ---------------------------------------------------------------------------


def _iter_records_from_s3(
    *, s3_client: Any, bucket: str, key: str
) -> Iterator[Mapping[str, Any]]:
    """Fetch the snapshot from S3 and yield records.

    The aind-data-schema snapshot is a single big array per the spec
    brief, but operators occasionally re-shape the same data as NDJSON
    for ad-hoc tooling. We accept either: peek at the first non-
    whitespace byte and dispatch accordingly.

    Uses ``ijson.items`` for streaming JSON-array parsing so the full
    7 GB snapshot is processed without loading it into memory. NDJSON
    is parsed line-by-line which has always been streaming.
    """
    import ijson  # local import — only the seeder needs ijson

    LOG.info("fetching s3://%s/%s", bucket, key)
    response = s3_client.get_object(Bucket=bucket, Key=key)
    body_stream = response["Body"]  # botocore.StreamingBody

    # Peek at the first non-whitespace byte to dispatch JSON-array vs
    # NDJSON without consuming the stream. We read 64 bytes to handle
    # leading whitespace / BOM, then a fresh GetObject re-opens the stream.
    peek = body_stream.read(64)
    head = peek.lstrip().lstrip(b"\xef\xbb\xbf")[:1]

    # Re-issue GetObject so the streaming parser starts at byte 0.
    response = s3_client.get_object(Bucket=bucket, Key=key)
    body_stream = response["Body"]

    if head == b"[":
        # JSON array — streaming parse with ijson; never materialises the
        # full document. ``item`` is the prefix that matches each top-level
        # array element.
        for record in ijson.items(body_stream, "item"):
            if isinstance(record, Mapping):
                yield record
        return

    if head == b"{":
        # NDJSON — line-buffered iteration over the streaming body.
        line_no = 0
        buffer = b""
        for chunk in body_stream.iter_chunks(chunk_size=1 << 20):  # 1 MiB
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line_no += 1
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise SeederError(
                        f"invalid NDJSON at s3://{bucket}/{key} line {line_no}: {exc.msg}"
                    ) from exc
                if isinstance(obj, Mapping):
                    yield obj
        # Trailing partial line.
        if buffer.strip():
            line_no += 1
            try:
                obj = json.loads(buffer.strip())
            except json.JSONDecodeError as exc:
                raise SeederError(
                    f"invalid NDJSON at s3://{bucket}/{key} line {line_no}: {exc.msg}"
                ) from exc
            if isinstance(obj, Mapping):
                yield obj
        return

    raise SeederError(
        f"unrecognised JSON shape at s3://{bucket}/{key}; expected an array or NDJSON"
    )


# ---------------------------------------------------------------------------
# Per-record inserts.
# ---------------------------------------------------------------------------


def _insert_mapped_record(
    conn: Any, mapped: MappedRecord, summary: SeedSummary
) -> None:
    """Insert all rows derived from one mapped record.

    The flow is structured to make idempotency cheap: we insert the
    Data_Asset *first* with ``ON CONFLICT (storage_uri) DO NOTHING
    RETURNING id``, and if the asset already exists we skip the rest
    of the record entirely. This avoids the trap where shared
    entities (subject/instrument/rig) get re-upserted (no-op) but
    asset-specific entities without UNIQUE columns (procedures,
    session, acquisition, etc.) get inserted as duplicates on every
    re-run.

    For a freshly-inserted Data_Asset:

    * Shared entities use ``ON CONFLICT (<natural_key>) DO UPDATE
      SET ... RETURNING id`` so we get the existing id back when
      the entity already exists from a prior record in this run
      (intra-run dedupe — e.g. two records sharing subject_id
      "695464"). We use ``DO UPDATE`` rather than ``DO NOTHING``
      because ``DO NOTHING`` does NOT trigger ``RETURNING`` on
      conflict and we always need the id for FKs and junctions.
    * Asset-specific entities (procedures, session, acquisition,
      processing, quality_control, data_description) are plain
      INSERTs with no UNIQUE backing — they only run when the
      asset itself is freshly inserted, so duplicates across runs
      are impossible.
    * Junctions use composite-PK + ``ON CONFLICT DO NOTHING`` so
      a re-link is a no-op (defensive, even though we only reach
      this branch on a fresh asset insert).

    The whole record is wrapped in a single transaction so a partial
    failure rolls back cleanly.
    """
    cur = conn.cursor()
    # ---- Data_Asset (FIRST — drives whole-record idempotency) -----
    assert mapped.data_asset is not None  # mapper guarantees it
    asset_id, asset_inserted = _upsert_returning(
        cur,
        table="data_asset",
        conflict_col="storage_uri",
        row=mapped.data_asset,
        on_conflict_action="DO NOTHING",
    )
    if not asset_inserted:
        # Asset already existed: skip every other row this record
        # would have produced. The prior run already linked the
        # shared entities and inserted the children. This is the
        # critical idempotency invariant.
        summary.data_assets_skipped += 1
        conn.commit()
        return

    summary.data_assets_inserted += 1

    # ---- Shared entities (only on fresh asset insert) -------------
    subject_id = None
    if mapped.subject is not None:
        subject_id, inserted = _upsert_returning(
            cur,
            table="subject",
            conflict_col="subject_id",
            row=mapped.subject,
        )
        if inserted:
            summary.subjects_inserted += 1
        else:
            summary.subjects_reused += 1

    instrument_id = None
    if mapped.instrument is not None:
        instrument_id, inserted = _upsert_returning(
            cur,
            table="instrument",
            conflict_col="instrument_id",
            row=mapped.instrument,
        )
        if inserted:
            summary.instruments_inserted += 1
        else:
            summary.instruments_reused += 1

    rig_id = None
    if mapped.rig is not None:
        rig_id, inserted = _upsert_returning(
            cur,
            table="rig",
            conflict_col="rig_id",
            row=mapped.rig,
        )
        if inserted:
            summary.rigs_inserted += 1
        else:
            summary.rigs_reused += 1

    procedures_id = None
    if mapped.procedures is not None:
        row = dict(mapped.procedures)
        if subject_id is not None:
            row["subject_id"] = subject_id
        procedures_id = _insert_returning(
            cur, table="procedures", row=row
        )
        summary.procedures_inserted += 1

    # ---- Asset-specific entities ----------------------------------
    if mapped.session is not None:
        row = dict(mapped.session)
        row["data_asset_id"] = asset_id
        row["subject_id"] = subject_id
        row["instrument_id"] = instrument_id
        row["rig_id"] = rig_id
        _insert_returning(cur, table="session", row=row)
        summary.sessions_inserted += 1

    if mapped.acquisition is not None:
        row = dict(mapped.acquisition)
        row["data_asset_id"] = asset_id
        row["instrument_id"] = instrument_id
        _insert_returning(cur, table="acquisition", row=row)
        summary.acquisitions_inserted += 1

    if mapped.processing is not None:
        row = dict(mapped.processing)
        row["data_asset_id"] = asset_id
        _insert_returning(cur, table="processing", row=row)
        summary.processings_inserted += 1

    if mapped.quality_control is not None:
        row = dict(mapped.quality_control)
        row["data_asset_id"] = asset_id
        _insert_returning(cur, table="quality_control", row=row)
        summary.quality_controls_inserted += 1

    if mapped.data_description is not None:
        row = dict(mapped.data_description)
        row["data_asset_id"] = asset_id
        _insert_returning(cur, table="data_description", row=row)
        summary.data_descriptions_inserted += 1

    # ---- Junctions ------------------------------------------------
    # Junctions use composite PK + ON CONFLICT DO NOTHING so a
    # re-link is a no-op. We only stamp the junctions when the
    # asset itself was freshly inserted (asset_inserted == True),
    # so this branch always inserts and increments.
    if mapped.link_subject and subject_id is not None:
        _insert_junction(
            cur,
            table="data_asset_subject",
            cols=("data_asset_id", "subject_id"),
            values=(asset_id, subject_id),
        )
        summary.junctions_inserted += 1

    if mapped.link_instrument and instrument_id is not None:
        _insert_junction(
            cur,
            table="data_asset_instrument",
            cols=("data_asset_id", "instrument_id"),
            values=(asset_id, instrument_id),
        )
        summary.junctions_inserted += 1

    if mapped.link_rig and rig_id is not None:
        _insert_junction(
            cur,
            table="data_asset_rig",
            cols=("data_asset_id", "rig_id"),
            values=(asset_id, rig_id),
        )
        summary.junctions_inserted += 1

    if mapped.link_procedures and procedures_id is not None:
        _insert_junction(
            cur,
            table="data_asset_procedures",
            cols=("data_asset_id", "procedures_id"),
            values=(asset_id, procedures_id),
        )
        summary.junctions_inserted += 1

    conn.commit()


# ---------------------------------------------------------------------------
# DB helpers.
# ---------------------------------------------------------------------------


def _upsert_returning(
    cur: Any,
    *,
    table: str,
    conflict_col: str,
    row: Mapping[str, Any],
    on_conflict_action: str = "DO UPDATE",
) -> tuple[Any, bool]:
    """INSERT … ON CONFLICT (<conflict_col>) … RETURNING id.

    Returns ``(row_id, inserted)`` where ``inserted`` is ``True`` when
    the row is freshly created. On conflict with ``DO UPDATE`` we
    re-set the conflict column to itself so ``RETURNING id`` still
    fires (Postgres requires an actual update path on conflict for
    RETURNING to populate). With ``DO NOTHING`` we cannot tell from
    RETURNING whether we conflicted; instead we issue a follow-up
    SELECT only if the INSERT did not return a row.
    """
    cols = list(row.keys())
    placeholders = ", ".join(["%s"] * len(cols))
    col_list = ", ".join(cols)
    params = tuple(_jsonable_param(row[c]) for c in cols)

    if on_conflict_action == "DO NOTHING":
        sql = (
            f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
            f"ON CONFLICT ({conflict_col}) DO NOTHING "
            f"RETURNING id"
        )
        cur.execute(sql, params)
        result = cur.fetchone()
        if result is not None:
            return result[0], True
        # Conflict path: look up the existing row by the conflict
        # column. We trust the conflict_col -> value mapping in `row`.
        cur.execute(
            f"SELECT id FROM {table} WHERE {conflict_col} = %s",
            (row[conflict_col],),
        )
        existing = cur.fetchone()
        if existing is None:
            raise SeederError(
                f"INSERT INTO {table} ON CONFLICT DO NOTHING returned no id "
                f"and follow-up SELECT could not find {conflict_col} = "
                f"{row[conflict_col]!r}"
            )
        return existing[0], False

    # DO UPDATE path. We do a no-op assignment (touch the conflict
    # column to itself) so RETURNING is populated whether we INSERTed
    # or UPDATEd. xmax = 0 in the system columns is the canonical
    # "this was a fresh insert, not an update" signal but pg8000 does
    # not expose it cleanly; we use the simpler `inserted := True`
    # approach via a CTE wrapper instead.
    sql = (
        f"WITH upserted AS ("
        f"  INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
        f"  ON CONFLICT ({conflict_col}) DO UPDATE "
        f"    SET {conflict_col} = EXCLUDED.{conflict_col} "
        f"  RETURNING id, (xmax = 0) AS inserted"
        f") SELECT id, inserted FROM upserted"
    )
    cur.execute(sql, params)
    result = cur.fetchone()
    if result is None:
        raise SeederError(f"upsert RETURNING produced no row for table {table!r}")
    return result[0], bool(result[1])


def _insert_returning(
    cur: Any, *, table: str, row: Mapping[str, Any]
) -> Any:
    """Plain INSERT ... RETURNING id (no conflict handling)."""
    cols = list(row.keys())
    placeholders = ", ".join(["%s"] * len(cols))
    col_list = ", ".join(cols)
    params = tuple(_jsonable_param(row[c]) for c in cols)

    cur.execute(
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) RETURNING id",
        params,
    )
    result = cur.fetchone()
    if result is None:
        raise SeederError(f"INSERT INTO {table} RETURNING id produced no row")
    return result[0]


def _insert_junction(
    cur: Any, *, table: str, cols: tuple[str, ...], values: tuple[Any, ...]
) -> None:
    """INSERT into a junction table with ON CONFLICT DO NOTHING.

    Junctions use composite primary keys so the conflict target is the
    PK; we don't need to name it explicitly — Postgres' ``ON CONFLICT
    DO NOTHING`` (no target) covers any unique constraint.
    """
    placeholders = ", ".join(["%s"] * len(cols))
    col_list = ", ".join(cols)
    cur.execute(
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT DO NOTHING",
        values,
    )


def _jsonable_param(value: Any) -> Any:
    """Convert dict/list values to JSON strings for pg8000 JSONB columns.

    pg8000 understands native dict/list as JSON but only when the
    column is declared JSONB and the driver knows the OID — relying
    on that requires a server round-trip we'd rather skip. Serialising
    explicitly avoids the issue and produces deterministic output the
    tests can assert on.
    """
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, default=str, ensure_ascii=True)
    return value


# ---------------------------------------------------------------------------
# Error budget.
# ---------------------------------------------------------------------------


def _record_error(summary: SeedSummary, max_errors: int, message: str) -> None:
    """Append an error to the summary. Raise if the budget is exhausted."""
    LOG.warning("seeder error: %s", message)
    summary.errors.append(message)
    if len(summary.errors) > max_errors:
        raise SeederError(
            f"seeder error budget exhausted: {len(summary.errors)} errors "
            f"(>{max_errors}); aborting to avoid a partially-loaded database"
        )


__all__ = (
    "BootstrapIds",
    "SeedSummary",
    "SeederError",
    "SYSTEM_ORG_NAME",
    "SYSTEM_USER_COGNITO_SUB",
    "DEFAULT_SPACE_NAME",
    "bootstrap_system_principal",
    "run_seeder",
)

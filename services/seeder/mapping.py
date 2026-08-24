"""
Allen BioData Registry PoC — record-to-row mapping for the seeder Lambda.

This module is the pure-functional layer between the aind-data-schema
record format on disk (S3 JSON) and the relational rows the seeder
INSERTs into Aurora. It deliberately knows nothing about the database
or about S3 — every function here takes a Python ``dict`` and returns
either a Python ``dict`` (representing one row's column values) or
``None`` (the field group is missing from the source record).

Design intent
-------------

* **Best-effort field mapping.** The 7 GB JSON snapshot is the
  authoritative aind-data-schema corpus the customer provided, but its
  exact field-by-field shape is not documented in this repo's design
  doc. The seeder is a PoC bring-up tool — its job is to produce a
  sensible relational graph for QC1 demos, not to perfectly preserve
  every aind-data-schema field. The full record (modulo redactions)
  is preserved in the ``metadata`` JSONB column on every entity, so
  any field we did not promote to a column is still queryable from
  Aurora and propagated through CDC to DocumentDB and OpenSearch.

* **Forgiving inputs.** The mapper tolerates missing optional fields,
  unexpected field names (we look in several plausible locations
  before giving up), and totally absent sub-objects. Required fields
  for which there is no plausible source value are filled with safe
  placeholders so the row inserts cleanly — the alternative (skipping
  the record) would silently shrink the seed corpus and hide data-
  shape bugs.

* **Idempotency-friendly outputs.** Where the registry has a UNIQUE
  constraint that drives idempotency (``data_asset.storage_uri``,
  ``subject.subject_id``, ``instrument.instrument_id``,
  ``rig.rig_id``), the mapper either pulls the natural key from the
  record or synthesises a deterministic key from the record's content
  hash. The seeder relies on this so re-runs are pure no-ops via
  ``ON CONFLICT (...) DO NOTHING``.

* **Asset-specific entities are 1:1 here.** ``session``,
  ``acquisition``, ``processing``, ``quality_control``, and
  ``data_description`` are tied to a single Data_Asset (per
  design.md §Overview.Guiding Principles + Property 10). The mapper
  emits at most one row per kind per record; if the source carries
  multiple sessions we keep the first and stash the rest into the
  Data_Asset's ``metadata`` blob.

Validates
---------

R32.2 (sample data loaded), R32.5 (idempotent re-run via content hash
+ deterministic natural keys).

Design references
-----------------

* design.md §IaC.Idempotency and Sample Data
* design.md §Effort Estimation.Data Seeding
* design.md §Overview.Guiding Principles ("Shared vs asset-specific
  entities").
* migrations/0002_data_asset.sql (column definitions every mapping
  output here is shaped against).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public dataclasses describing what one source record maps to.
# ---------------------------------------------------------------------------


@dataclass
class MappedRecord:
    """All rows derivable from one source aind-data-schema record.

    Each attribute is either ``None`` (no row to insert) or a ``dict``
    of column-name -> value. The seeder consumes this dataclass and
    issues parameterized INSERT statements — it does not need to know
    the field set ahead of time, which keeps the mapping schema-free
    and easy to evolve.

    The ``content_hash`` is the SHA-256 of the canonicalised source
    record. It is stored on the Data_Asset's ``metadata`` JSONB blob
    under the key ``__seeder_content_hash`` so a future seeder run
    can detect when the same logical record has been re-shaped on
    disk (and skip it explicitly rather than relying on the unique
    URI constraint).
    """

    content_hash: str
    data_asset: Optional[Dict[str, Any]] = None
    subject: Optional[Dict[str, Any]] = None
    instrument: Optional[Dict[str, Any]] = None
    rig: Optional[Dict[str, Any]] = None
    procedures: Optional[Dict[str, Any]] = None
    session: Optional[Dict[str, Any]] = None
    acquisition: Optional[Dict[str, Any]] = None
    processing: Optional[Dict[str, Any]] = None
    quality_control: Optional[Dict[str, Any]] = None
    data_description: Optional[Dict[str, Any]] = None
    # Junctions are stamped post-insert by the seeder once it has the
    # asset & shared-entity ids; the mapper just signals which shared
    # entities should be linked.
    link_subject: bool = False
    link_instrument: bool = False
    link_rig: bool = False
    link_procedures: bool = False
    # Warnings the mapper accumulated while shaping the row. Surfaced
    # in the seeder summary so the operator can see how many records
    # had degenerate shapes.
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public entry points.
# ---------------------------------------------------------------------------


def compute_content_hash(record: Mapping[str, Any]) -> str:
    """Return the SHA-256 hex digest of the record's canonical JSON form.

    The canonical form is ``json.dumps(record, sort_keys=True,
    separators=(",", ":"), ensure_ascii=True, default=str)`` — sorted
    keys make the digest stable across Python versions and dict
    insertion orders. ``default=str`` is a safety net for non-JSON-
    serialisable values (datetimes, decimals); they are rare in
    aind-data-schema records but show up occasionally in the
    ``metadata`` blob.
    """
    canonical = json.dumps(
        record,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def should_sample(content_hash: str, fraction: float) -> bool:
    """Deterministically decide whether to keep this record for seeding.

    Hashes the record's content and compares the first 8 hex chars
    (32 bits) modulo 1,000,000 against ``fraction * 1,000,000``. The
    sample is therefore:

    * **Deterministic** — same record, same fraction → same decision,
      across runs and across machines. Re-running the seeder produces
      the exact same subset, which is what the idempotency contract
      requires.
    * **Uniform** — content hashes are uniformly distributed by
      construction, so the sampled subset is statistically
      representative without any explicit shuffling.
    * **Cheap** — no global state, no RNG seeding, no preprocessing
      pass.

    ``fraction`` outside the inclusive ``[0.0, 1.0]`` range is clamped:
    negatives behave like 0 (always reject), values >= 1 behave like
    a no-op (always keep). 1.0 keeps everything.
    """
    if fraction <= 0.0:
        return False
    if fraction >= 1.0:
        return True
    # Take the leading 8 hex chars (32 bits). Modulo 1_000_000 gives a
    # value in [0, 999_999]; comparing against fraction * 1_000_000
    # gives a per-record acceptance probability that matches `fraction`
    # to within 1 ppm — well below the noise of a 10% sample.
    bucket = int(content_hash[:8], 16) % 1_000_000
    return bucket < int(fraction * 1_000_000)


def map_record(
    record: Mapping[str, Any],
    *,
    space_id: str,
    created_by: str,
) -> Optional[MappedRecord]:
    """Map a single source record into all the rows it contributes.

    Parameters
    ----------
    record:
        The decoded JSON record from the snapshot. Must be a dict; non-
        dict inputs (or completely empty dicts) yield ``None`` because
        there is no anchor to build a Data_Asset row around.
    space_id:
        UUID of the Space the resulting Data_Asset belongs to. The
        seeder bootstraps a default Space and passes its id here.
    created_by:
        UUID of the ``app_user`` that owns the seeded rows. The seeder
        bootstraps a system user and passes its id here.

    Returns
    -------
    A :class:`MappedRecord`, or ``None`` if the input is unmappable.

    The function is total over plausible aind-data-schema shapes — it
    does not raise on missing fields. Validation errors from
    ``aind-data-schema`` itself are NOT enforced here; the seeder is a
    bring-up tool, not the validation pipeline. Records that fail
    validation will land in Aurora with ``validation_status =
    'unvalidated'`` and the actual Validation_Lambda (Task 21) will
    re-validate them as part of QC1.
    """
    if not isinstance(record, Mapping) or len(record) == 0:
        return None

    content_hash = compute_content_hash(record)

    # ---- 1) Data_Asset --------------------------------------------------
    storage_uri = _extract_storage_uri(record)
    if storage_uri is None:
        # Without a storage URI we cannot honour the unique-key
        # idempotency contract — synthesise one from the content hash
        # so re-runs still produce stable rows. The synthetic URI uses
        # a sentinel scheme so the customer can grep for "no real URI"
        # cases after the seed.
        storage_uri = f"seed://no-storage-uri/{content_hash}"

    name = _first_non_empty_str(
        record.get("name"),
        record.get("data_asset_name"),
        (record.get("data_description") or {}).get("name"),
        (record.get("data_description") or {}).get("data_asset_name"),
    )
    description = _first_non_empty_str(
        record.get("description"),
        (record.get("data_description") or {}).get("description"),
    )
    data_type = _first_non_empty_str(
        record.get("data_type"),
        record.get("modality"),
        (record.get("data_description") or {}).get("modality"),
    )
    schema_version = _first_non_empty_str(
        record.get("schema_version"),
        record.get("aind_data_schema_version"),
        (record.get("data_description") or {}).get("schema_version"),
    )

    # The full source record (modulo top-level fields we already
    # promoted) goes into metadata. We tag the seeder-specific
    # bookkeeping under a ``__seeder`` key so it does not collide with
    # any aind-data-schema field name.
    metadata_blob = {
        "__seeder": {
            "content_hash": content_hash,
            "source": "seed_lambda",
            "schema_version_seen_at_seed": schema_version,
        },
        "source_record": _safe_jsonable(dict(record)),
    }

    data_asset_row: Dict[str, Any] = {
        "space_id": space_id,
        "name": name,
        "display_name": name,
        "storage_uri": storage_uri,
        "data_type": data_type,
        # Default lifecycle/validation states match the column defaults
        # in 0002_data_asset.sql — kept explicit here so a reader sees
        # the seeder is intentionally landing rows in 'draft' /
        # 'unvalidated' for the validation pipeline to pick up.
        "lifecycle_state": "draft",
        "validation_status": "unvalidated",
        "schema_version": schema_version,
        "description": description,
        "metadata": metadata_blob,
        "created_by": created_by,
    }

    mapped = MappedRecord(content_hash=content_hash, data_asset=data_asset_row)

    # ---- 2) Shared entities --------------------------------------------
    subject_obj = _coerce_dict(record.get("subject"))
    if subject_obj is not None:
        subject_row = _map_subject(subject_obj, created_by=created_by)
        if subject_row is not None:
            mapped.subject = subject_row
            mapped.link_subject = True

    instrument_obj = _coerce_dict(record.get("instrument"))
    if instrument_obj is not None:
        instrument_row = _map_instrument(instrument_obj, created_by=created_by)
        if instrument_row is not None:
            mapped.instrument = instrument_row
            mapped.link_instrument = True

    rig_obj = _coerce_dict(record.get("rig"))
    if rig_obj is not None:
        rig_row = _map_rig(rig_obj, created_by=created_by)
        if rig_row is not None:
            mapped.rig = rig_row
            mapped.link_rig = True

    procedures_obj = _coerce_dict(record.get("procedures"))
    if procedures_obj is not None:
        procedures_row = _map_procedures(procedures_obj, created_by=created_by)
        if procedures_row is not None:
            mapped.procedures = procedures_row
            mapped.link_procedures = True

    # ---- 3) Asset-specific entities ------------------------------------
    session_obj = _coerce_dict(record.get("session"))
    if session_obj is not None:
        mapped.session = _map_session(session_obj)

    acquisition_obj = _coerce_dict(record.get("acquisition"))
    if acquisition_obj is not None:
        mapped.acquisition = _map_acquisition(acquisition_obj)

    processing_obj = _coerce_dict(record.get("processing"))
    if processing_obj is not None:
        mapped.processing = _map_processing(processing_obj)

    qc_obj = _coerce_dict(record.get("quality_control"))
    if qc_obj is not None:
        mapped.quality_control = _map_quality_control(qc_obj)

    dd_obj = _coerce_dict(record.get("data_description"))
    if dd_obj is not None:
        mapped.data_description = _map_data_description(dd_obj)

    return mapped


# ---------------------------------------------------------------------------
# Per-entity mappers.
# ---------------------------------------------------------------------------


def _map_subject(
    obj: Mapping[str, Any], *, created_by: str
) -> Optional[Dict[str, Any]]:
    """Map an aind-data-schema Subject to a ``subject`` row.

    ``subject_id`` is the UNIQUE column on the table — without it we
    have no idempotency key, so we synthesise one from the object's
    content hash. Every field except ``subject_id``, ``species``, and
    ``created_by`` is optional and defaults to None.
    """
    subject_id = _first_non_empty_str(
        obj.get("subject_id"), obj.get("id"), obj.get("subject_name")
    ) or f"seed-subject-{compute_content_hash(obj)[:16]}"

    species = _first_non_empty_str(obj.get("species"), obj.get("organism")) or "unknown"

    return {
        "subject_id": subject_id,
        "species": species,
        "sex": _first_non_empty_str(obj.get("sex")),
        "date_of_birth": _coerce_date(obj.get("date_of_birth"), obj.get("dob")),
        "genotype": _first_non_empty_str(obj.get("genotype")),
        "source": _first_non_empty_str(obj.get("source"), obj.get("provider")),
        "weight_at_acquisition_g": _coerce_decimal(
            obj.get("weight_at_acquisition_g"), obj.get("weight_g"), obj.get("weight")
        ),
        "age_at_acquisition_days": _coerce_decimal(
            obj.get("age_at_acquisition_days"), obj.get("age_days")
        ),
        "notes": _first_non_empty_str(obj.get("notes")),
        "metadata": _safe_jsonable(dict(obj)),
        "created_by": created_by,
    }


def _map_instrument(
    obj: Mapping[str, Any], *, created_by: str
) -> Optional[Dict[str, Any]]:
    """Map an aind-data-schema Instrument to an ``instrument`` row."""
    instrument_id = _first_non_empty_str(
        obj.get("instrument_id"), obj.get("id"), obj.get("name")
    ) or f"seed-instrument-{compute_content_hash(obj)[:16]}"

    return {
        "instrument_id": instrument_id,
        "instrument_type": _first_non_empty_str(
            obj.get("instrument_type"), obj.get("type"), obj.get("kind")
        ),
        "manufacturer": _first_non_empty_str(obj.get("manufacturer")),
        "model": _first_non_empty_str(obj.get("model")),
        "serial_number": _first_non_empty_str(
            obj.get("serial_number"), obj.get("serial")
        ),
        "calibration_date": _coerce_date(obj.get("calibration_date")),
        "notes": _first_non_empty_str(obj.get("notes")),
        "metadata": _safe_jsonable(dict(obj)),
        "created_by": created_by,
    }


def _map_rig(obj: Mapping[str, Any], *, created_by: str) -> Optional[Dict[str, Any]]:
    """Map an aind-data-schema Rig to a ``rig`` row."""
    rig_id = _first_non_empty_str(
        obj.get("rig_id"), obj.get("id"), obj.get("name")
    ) or f"seed-rig-{compute_content_hash(obj)[:16]}"

    modalities_raw = obj.get("modalities") or obj.get("modality") or []
    if isinstance(modalities_raw, str):
        modalities = [modalities_raw]
    elif isinstance(modalities_raw, list):
        modalities = [str(m) for m in modalities_raw if m]
    else:
        modalities = []

    return {
        "rig_id": rig_id,
        "modalities": modalities,
        "location": _first_non_empty_str(obj.get("location"), obj.get("site")),
        "notes": _first_non_empty_str(obj.get("notes")),
        "metadata": _safe_jsonable(dict(obj)),
        "created_by": created_by,
    }


def _map_procedures(
    obj: Mapping[str, Any], *, created_by: str
) -> Optional[Dict[str, Any]]:
    """Map an aind-data-schema Procedures record to a ``procedures`` row.

    Note: ``procedures.subject_id`` is the FK to ``subject(id)`` (the
    UUID), not the natural ``subject_id`` string. The seeder fills it
    in post-insert once it has the subject row id, so the mapper omits
    it here.
    """
    return {
        "surgery_date": _coerce_date(
            obj.get("surgery_date"), obj.get("date"), obj.get("performed_date")
        ),
        "protocol": _first_non_empty_str(obj.get("protocol"), obj.get("protocol_id")),
        "performed_by": _first_non_empty_str(
            obj.get("performed_by"), obj.get("operator")
        ),
        "notes": _first_non_empty_str(obj.get("notes")),
        "metadata": _safe_jsonable(dict(obj)),
        "created_by": created_by,
    }


def _map_session(obj: Mapping[str, Any]) -> Dict[str, Any]:
    """Map an aind-data-schema Session to a ``session`` row.

    ``data_asset_id``, ``subject_id``, ``instrument_id``, ``rig_id`` are
    filled in post-insert by the seeder (it has the row ids by then).
    """
    return {
        "session_id": _first_non_empty_str(obj.get("session_id"), obj.get("id")),
        "session_type": _first_non_empty_str(
            obj.get("session_type"), obj.get("type")
        ),
        "session_start": _coerce_timestamp(
            obj.get("session_start"), obj.get("start_time"), obj.get("start")
        ),
        "session_end": _coerce_timestamp(
            obj.get("session_end"), obj.get("end_time"), obj.get("end")
        ),
        "experimenter": _first_non_empty_str(
            obj.get("experimenter"), obj.get("experimenters")
        ),
        "notes": _first_non_empty_str(obj.get("notes")),
        "metadata": _safe_jsonable(dict(obj)),
    }


def _map_acquisition(obj: Mapping[str, Any]) -> Dict[str, Any]:
    """Map an aind-data-schema Acquisition to an ``acquisition`` row."""
    return {
        "acquisition_start": _coerce_timestamp(
            obj.get("acquisition_start"), obj.get("start_time")
        ),
        "acquisition_end": _coerce_timestamp(
            obj.get("acquisition_end"), obj.get("end_time")
        ),
        "parameters": _safe_jsonable(_coerce_dict(obj.get("parameters")) or {}),
        "notes": _first_non_empty_str(obj.get("notes")),
        "metadata": _safe_jsonable(dict(obj)),
    }


def _map_processing(obj: Mapping[str, Any]) -> Dict[str, Any]:
    """Map an aind-data-schema Processing to a ``processing`` row."""
    return {
        "processing_pipeline": _first_non_empty_str(
            obj.get("processing_pipeline"), obj.get("pipeline_name"), obj.get("name")
        ),
        "version": _first_non_empty_str(obj.get("version"), obj.get("pipeline_version")),
        "parameters": _safe_jsonable(_coerce_dict(obj.get("parameters")) or {}),
        "notes": _first_non_empty_str(obj.get("notes")),
        "started_at": _coerce_timestamp(obj.get("started_at"), obj.get("start_time")),
        "completed_at": _coerce_timestamp(
            obj.get("completed_at"), obj.get("end_time"), obj.get("finished_at")
        ),
        "metadata": _safe_jsonable(dict(obj)),
    }


def _map_quality_control(obj: Mapping[str, Any]) -> Dict[str, Any]:
    """Map an aind-data-schema QualityControl to a ``quality_control`` row.

    aind-data-schema QC carries either a single metric or an evaluation
    bundle; we collapse to a single row here (the most representative
    metric) and stash the rest in ``metadata``. A future iteration that
    needs full QC fidelity should fan out to multiple rows.
    """
    return {
        "qc_metric": _first_non_empty_str(obj.get("qc_metric"), obj.get("metric")),
        "value": _coerce_decimal(obj.get("value")),
        "unit": _first_non_empty_str(obj.get("unit")),
        "status": _first_non_empty_str(obj.get("status"), obj.get("result")),
        "notes": _first_non_empty_str(obj.get("notes")),
        "metadata": _safe_jsonable(dict(obj)),
    }


def _map_data_description(obj: Mapping[str, Any]) -> Dict[str, Any]:
    """Map an aind-data-schema DataDescription to a ``data_description`` row."""
    return {
        "description_kind": "human",
        "text": _first_non_empty_str(obj.get("text"), obj.get("description")),
        "language": _first_non_empty_str(obj.get("language")) or "en",
        "funding_source": _first_non_empty_str(
            obj.get("funding_source"), obj.get("funding")
        ),
        "license": _first_non_empty_str(obj.get("license")),
        "metadata": _safe_jsonable(dict(obj)),
    }


# ---------------------------------------------------------------------------
# Tiny helpers (kept here so the mapper stays a single self-contained file).
# ---------------------------------------------------------------------------


def _first_non_empty_str(*candidates: Any) -> Optional[str]:
    """Return the first candidate that stringifies to a non-empty,
    non-whitespace value. Returns ``None`` if none qualify."""
    for c in candidates:
        if c is None:
            continue
        if isinstance(c, str):
            stripped = c.strip()
            if stripped:
                return stripped
        else:
            # Fall back to str() — handles ints, UUIDs, etc.
            s = str(c).strip()
            if s and s.lower() != "none":
                return s
    return None


def _coerce_dict(value: Any) -> Optional[Dict[str, Any]]:
    """Coerce a value to a dict, or return ``None`` if it is not dict-shaped.

    aind-data-schema records sometimes carry empty objects ``{}`` to
    signal "field was present but unset"; we treat those as absent.
    """
    if isinstance(value, Mapping) and len(value) > 0:
        return dict(value)
    return None


def _coerce_date(*candidates: Any) -> Optional[str]:
    """Return the first candidate that looks like an ISO date (YYYY-MM-DD).

    pg8000 accepts ISO date strings for DATE columns directly, so we
    keep them as strings rather than parsing into Python ``date`` —
    this avoids the timezone-correctness rabbit hole at seed time.
    """
    for c in candidates:
        s = _first_non_empty_str(c)
        if s is None:
            continue
        # Accept the first 10 chars if they parse like YYYY-MM-DD;
        # ignore time components — we only have a DATE column.
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            head = s[:10]
            year, month, day = head.split("-")
            if year.isdigit() and month.isdigit() and day.isdigit():
                return head
    return None


def _coerce_timestamp(*candidates: Any) -> Optional[str]:
    """Return the first candidate that looks like an ISO timestamp.

    Like ``_coerce_date``, we keep the value as a string — pg8000
    accepts ISO 8601 directly for ``timestamptz`` columns. We do a
    very loose shape check (must contain a 'T' or ' ' separator after
    the date portion); anything that fails it falls through.
    """
    for c in candidates:
        s = _first_non_empty_str(c)
        if s is None:
            continue
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            # Pure date is also acceptable — Postgres will midnight it.
            if len(s) == 10:
                return s
            # Has a time portion — accept any common separator.
            if s[10] in ("T", "t", " "):
                return s
    return None


def _coerce_decimal(*candidates: Any) -> Optional[float]:
    """Return the first candidate that parses as a float.

    pg8000 accepts Python floats for NUMERIC columns. The function
    intentionally ignores invalid values rather than raising — the
    seeder is best-effort.
    """
    for c in candidates:
        if c is None:
            continue
        if isinstance(c, (int, float)) and not isinstance(c, bool):
            return float(c)
        if isinstance(c, str):
            try:
                return float(c)
            except ValueError:
                continue
    return None


def _safe_jsonable(value: Any) -> Any:
    """Strip values that ``json.dumps`` cannot serialise.

    pg8000 sends JSONB by serialising a Python value through
    ``json.dumps``. Records may carry unusual types (datetimes,
    Decimals, sets) that would crash the encoder. We recursively
    coerce them to strings so the row insert always succeeds.
    """
    try:
        # Fast path: if json.dumps already accepts it, no rewrite.
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        pass

    if isinstance(value, Mapping):
        return {str(k): _safe_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_jsonable(v) for v in value]
    return str(value)


def _extract_storage_uri(record: Mapping[str, Any]) -> Optional[str]:
    """Find the storage URI in any of the plausible field locations.

    The aind-data-schema corpus places it variously at
    ``location.s3_uri``, ``s3_uri``, ``storage_uri``, ``location``
    (when ``location`` is itself a string), and a few other places.
    We try them in priority order — the first non-empty hit wins.
    """
    location = record.get("location")
    if isinstance(location, Mapping):
        for key in ("s3_uri", "uri", "storage_uri", "url"):
            value = _first_non_empty_str(location.get(key))
            if value:
                return value
    elif isinstance(location, str):
        s = location.strip()
        if s:
            return s

    for key in ("storage_uri", "s3_uri", "uri", "url"):
        value = _first_non_empty_str(record.get(key))
        if value:
            return value

    return None


__all__ = (
    "MappedRecord",
    "compute_content_hash",
    "map_record",
    "should_sample",
)

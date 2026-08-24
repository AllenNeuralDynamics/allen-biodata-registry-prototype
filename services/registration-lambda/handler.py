"""
Allen BioData Registry PoC — Registration Lambda (core CRUD).

This Lambda is the API Gateway-fronted entry point for every CRUD
operation on Data_Assets and Metadata_Entities. It implements:

* ``POST   /assets``               — create a Data_Asset.
* ``GET    /assets/{id}``          — read a Data_Asset (RLS-filtered).
* ``PUT    /assets/{id}``          — update a Data_Asset (creates a revision).
* ``POST   /entities/{type}``      — create a Metadata_Entity (polymorphic over
                                      subject / instrument / rig / procedures /
                                      session / acquisition / processing /
                                      quality_control / data_description).
* ``GET    /entities/{type}/{id}`` — read a Metadata_Entity.
* ``PUT    /entities/{type}/{id}`` — update a Metadata_Entity (creates a revision).

What this Lambda does (and what it intentionally does NOT do)
-------------------------------------------------------------

**Does**:

1. Parse + validate the request body against the hand-authored
   OpenAPI 3.0 spec (``openapi/openapi.yaml``) using the shared
   middleware. aind-data-schema Pydantic models drive deep schema
   validation indirectly via the ``$ref``-resolved JSON Schemas in
   ``openapi/components/schemas/``.
2. Open an Aurora connection through ``aurora_connection`` from the
   shared Layer. The helper sets the four ``app.current_*`` GUCs
   from the caller's :class:`AuthContext` so Postgres RLS is in
   effect for every subsequent statement (Layer 2 of the three-layer
   RLS model).
3. INSERT / UPDATE / SELECT against the source-of-truth Aurora
   tables. On every write, append exactly one immutable
   ``entity_revision`` row in the same transaction, with
   ``change_source`` derived from the request:
     - ``manual`` (default) — interactive UI write.
     - ``agent`` when the caller forwards ``X-Agent-Source: true``
       (the Agent UI sets this header so MetaData_Agent edits are
       attributable as agent-driven).
     - ``api``   when the caller forwards ``X-API-Source: true``
       (programmatic third-party clients).
4. Apply the Layer-3 :func:`check_sensitive_flag` guard on direct
   GETs so callers without sensitive privileges receive 403
   ``SENSITIVE_ACCESS_DENIED`` instead of the row.
5. Map every error path to the canonical Property 14 envelope
   (``{code, message, details, request_id, timestamp}``) via
   :func:`error_response_from_exception`.

**Does NOT**:

* Write to DocumentDB or OpenSearch directly. CDC (Task 17, 18) reads
  Aurora's logical replication slot and fans the change out to both
  read stores asynchronously. Synchronous dual-writes would couple
  this Lambda's success path to two more services and break the
  eventual-consistency guarantee documented in design.md
  §Architecture.CDC Pipeline Architecture.
* Run aind-data-schema validation that produces a persisted
  ``validation_status``. Validation_Lambda (Task 21) owns that;
  Registration_Lambda preserves the existing ``validation_status``
  on PUT and writes ``unvalidated`` on POST so the caller knows the
  payload still needs to pass through ``POST /validate`` before it
  can be published.
* Run the synchronous duplicate-similarity check. Duplicates_Lambda
  (Task 25.1) will be invoked from here in a follow-up task; for
  now we let the database-level
  ``data_asset_storage_uri_unique`` constraint produce the only 409
  ``DUPLICATE_ENTITY`` path. The OpenAPI surface still returns the
  201 with a ``warnings: []`` array so the wire shape is stable;
  Task 25 fills the array.

Validates
---------

R1.1, R1.2, R1.4, R1.5, R1.6, R1.7, R2.4, R2.5, R2.6, R6.1, R6.2,
R28.2, R33.1, R33.2.

Design references
-----------------

* design.md §Components.2. Registration_Lambda.
* design.md §Architecture.RLS Enforcement Architecture.
* design.md §External Interfaces.API Gateway REST.
* design.md §Correctness Properties.Property 5 (JSONB round-trip).
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional, Sequence

from biodata_registry_shared import (
    AuthContext,
    AuthContextError,
    DuplicateEntity,
    Forbidden,
    MissingProvenance,
    NotFound,
    OpenAPIValidationError,
    PRIVILEGED_SENSITIVE_ROLES,
    RegistryError,
    Unauthorized,
    ValidationFailed,
    aurora_connection,
    bind_request_id,
    check_sensitive_flag,
    configure_logging,
    error_response_from_exception,
    get_logger,
    is_data_admin,
    load_spec,
    parse_auth_context,
    require_role,
    require_space_access,
    validate_event,
)

LOG = get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Closed set of Metadata_Entity types accepted on /entities/{type}.
# Mirrors design.md §Data Models.Aurora and the EntityType enum in
# services/openapi-types/biodata_registry_types.py. Kept as a frozenset
# (rather than imported from biodata_registry_types) so the Lambda
# doesn't have to bundle the openapi-types package — the registry
# governance Pydantic models only ship in the build-time tooling.
_VALID_ENTITY_TYPES: frozenset[str] = frozenset(
    {
        "subject",
        "instrument",
        "rig",
        "procedures",
        "session",
        "acquisition",
        "processing",
        "quality_control",
        "data_description",
    }
)

# Per-entity-table column allow-lists. The Lambda accepts arbitrary
# JSON bodies (the OpenAPI spec marks them ``additionalProperties:
# true`` for polymorphic /entities endpoints), but only **promoted**
# columns are written as relational fields. Anything else lands in
# the ``metadata`` JSONB passthrough column. This keeps the schema
# from being a moving target whenever aind-data-schema adds a new
# field — the new field is captured losslessly in JSONB without a
# DDL migration.
#
# The allow-lists are derived from migrations/0002_data_asset.sql.
# The order is irrelevant — INSERTs use named columns.
_DATA_ASSET_PROMOTED_COLUMNS: frozenset[str] = frozenset(
    {
        "name",
        "display_name",
        "storage_uri",
        "data_type",
        "validation_errors",
        "sensitive_flag",
        "sensitive_flag_meta",
        "schema_id",
        "schema_version",
        "provenance_source_id",
        "description",
    }
)

_ENTITY_PROMOTED_COLUMNS: dict[str, frozenset[str]] = {
    "subject": frozenset(
        {
            "subject_id",
            "species",
            "sex",
            "date_of_birth",
            "genotype",
            "source",
            "weight_at_acquisition_g",
            "age_at_acquisition_days",
            "notes",
        }
    ),
    "instrument": frozenset(
        {
            "instrument_id",
            "instrument_type",
            "manufacturer",
            "model",
            "serial_number",
            "calibration_date",
            "notes",
        }
    ),
    "rig": frozenset({"rig_id", "modalities", "location", "notes"}),
    "procedures": frozenset(
        {"subject_id", "surgery_date", "protocol", "performed_by", "notes"}
    ),
    "session": frozenset(
        {
            "data_asset_id",
            "session_id",
            "session_type",
            "session_start",
            "session_end",
            "experimenter",
            "subject_id",
            "instrument_id",
            "rig_id",
            "notes",
        }
    ),
    "acquisition": frozenset(
        {
            "data_asset_id",
            "session_id",
            "instrument_id",
            "acquisition_start",
            "acquisition_end",
            "parameters",
            "notes",
        }
    ),
    "processing": frozenset(
        {
            "data_asset_id",
            "processing_pipeline",
            "version",
            "parameters",
            "notes",
            "started_at",
            "completed_at",
        }
    ),
    "quality_control": frozenset(
        {
            "data_asset_id",
            "qc_metric",
            "value",
            "unit",
            "status",
            "notes",
        }
    ),
    "data_description": frozenset(
        {
            "data_asset_id",
            "description_kind",
            "text",
            "language",
            "funding_source",
            "license",
        }
    ),
}

# Tables whose ``created_by`` column is non-nullable. Every shared and
# asset-specific entity table EXCEPT session / acquisition / processing /
# quality_control / data_description carries it. The omitted tables only
# track ``created_at`` because they're owned via ``data_asset_id`` and
# CASCADE on delete — the asset's ``created_by`` is the audit anchor.
_TABLES_WITH_CREATED_BY: frozenset[str] = frozenset(
    {"data_asset", "subject", "instrument", "rig", "procedures"}
)

# Tables that carry a JSONB ``metadata`` passthrough. Every CRUD'd
# table on this Lambda has it.
_TABLES_WITH_METADATA: frozenset[str] = {
    "data_asset",
    *(_ENTITY_PROMOTED_COLUMNS.keys()),
}

# JSONB columns per table (other than the catch-all ``metadata``).
# Used during INSERT/UPDATE so we serialise dict/list into JSON text
# rather than trusting psycopg's type-inference on a generic untyped
# parameter.
_JSONB_COLUMNS: dict[str, frozenset[str]] = {
    "data_asset": frozenset({"validation_errors", "sensitive_flag_meta"}),
    "acquisition": frozenset({"parameters"}),
    "processing": frozenset({"parameters"}),
}


# OpenAPI spec path. The Lambda Terraform module bakes the spec into
# the deployment image at the same relative path. Override-able via
# the ``OPENAPI_SPEC_PATH`` env var for tests / local dev.
def _spec_path() -> str:
    return os.environ.get(
        "OPENAPI_SPEC_PATH",
        os.path.join(os.path.dirname(__file__), "openapi.yaml"),
    )


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------


def handler(event: Mapping[str, Any], context: Any) -> Mapping[str, Any]:
    """API Gateway proxy entry point.

    Routes the request to one of the six business operations based on
    ``(event['httpMethod'], event['resource'])`` and returns the API
    Gateway proxy response shape ``{statusCode, headers, body}``.
    """
    configure_logging()

    request_id = _extract_request_id(event, context)
    with bind_request_id(request_id):
        try:
            return _dispatch(event, request_id=request_id)
        except RegistryError as exc:
            # ``extra`` keys cannot collide with built-in LogRecord
            # attributes (``message`` etc.), so we prefix everything
            # with ``error_`` to keep the JSON formatter's output
            # unambiguous and Python's logging library happy.
            LOG.warning(
                "registry error",
                extra={
                    "error_code": exc.code.value,
                    "error_message": exc.message,
                    "error_http_status": exc.http_status,
                },
            )
            return error_response_from_exception(exc, request_id=request_id)
        except AuthContextError as exc:
            LOG.warning(
                "auth context parse failure",
                extra={"error_detail": str(exc)},
            )
            return error_response_from_exception(
                Unauthorized(str(exc)), request_id=request_id
            )
        except OpenAPIValidationError as exc:
            # Spec failed to load — operator-facing problem, not a
            # client problem. Surface 500 so the alarm fires.
            LOG.exception("openapi spec load failure")
            return error_response_from_exception(
                RegistryError(
                    f"OpenAPI spec failed to load: {exc}",
                ),
                request_id=request_id,
            )
        except Exception as exc:  # noqa: BLE001 — defense in depth
            LOG.exception("unhandled exception")
            return error_response_from_exception(
                RegistryError(
                    "Internal server error",
                    details={"error_type": type(exc).__name__},
                ),
                request_id=request_id,
            )


def _dispatch(
    event: Mapping[str, Any], *, request_id: str
) -> Mapping[str, Any]:
    """Top-level routing.

    The OpenAPI middleware validates the request shape against the
    spec; we then pattern-match on (method, resource) to pick the
    business operation. Routes the spec doesn't contain (e.g.
    ``/assets`` GET listing — currently delegated to other tasks)
    fall through to a 404.
    """
    auth = parse_auth_context(event)
    method = (event.get("httpMethod") or "").upper()
    resource = event.get("resource") or ""
    path_params = event.get("pathParameters") or {}

    # Validate the request body / query parameters against OpenAPI.
    # We only validate writes to keep GET hot-path latency unchanged.
    if method in ("POST", "PUT"):
        spec = load_spec(path=_spec_path())
        validate_event(spec, event)

    body = _parse_json_body(event)

    if method == "POST" and resource == "/assets":
        return _create_asset(auth, body, event, request_id=request_id)
    if method == "GET" and resource == "/assets/{id}":
        return _get_asset(
            auth, _require_path_param(path_params, "id"), request_id=request_id
        )
    if method == "PUT" and resource == "/assets/{id}":
        return _update_asset(
            auth,
            _require_path_param(path_params, "id"),
            body,
            event,
            request_id=request_id,
        )
    if method == "POST" and resource == "/entities/{type}":
        return _create_entity(
            auth,
            _require_path_param(path_params, "type"),
            body,
            event,
            request_id=request_id,
        )
    if method == "GET" and resource == "/entities/{type}/{id}":
        return _get_entity(
            auth,
            _require_path_param(path_params, "type"),
            _require_path_param(path_params, "id"),
            request_id=request_id,
        )
    if method == "PUT" and resource == "/entities/{type}/{id}":
        return _update_entity(
            auth,
            _require_path_param(path_params, "type"),
            _require_path_param(path_params, "id"),
            body,
            event,
            request_id=request_id,
        )

    raise NotFound(
        f"Route {method} {resource!r} is not handled by Registration_Lambda"
    )


# ---------------------------------------------------------------------------
# Business operations — Data_Asset
# ---------------------------------------------------------------------------


def _create_asset(
    auth: AuthContext,
    body: Mapping[str, Any],
    event: Mapping[str, Any],
    *,
    request_id: str,
) -> Mapping[str, Any]:
    """POST /assets — create a new Data_Asset.

    Validates business invariants (R1.5: derived assets must carry a
    provenance link), writes the row, and appends a revision in the
    same transaction. The synchronous duplicate-similarity check is
    left to Task 25.1; the only 409 path here is the
    ``data_asset_storage_uri_unique`` UNIQUE-violation backstop.
    """
    storage_uri = body.get("storage_uri")
    if not isinstance(storage_uri, str) or not storage_uri.strip():
        # OpenAPI middleware should have caught this; re-check defensively
        # so we can produce a stable 422 if the spec drifts.
        raise ValidationFailed(
            "Request body is missing required field 'storage_uri'",
            details=[
                {
                    "field": "body.storage_uri",
                    "rule": "required",
                    "message": "storage_uri is required for asset creation",
                }
            ],
        )

    # space_id resolution: explicit body field wins; otherwise fall
    # back to the caller's only space if they have exactly one.
    # When the caller has no space membership (rare — admins coming in
    # via a sharing grant) we surface a 400 so they pick one.
    space_id = body.get("space_id")
    if space_id is None:
        candidate = next(iter(sorted(auth.space_ids)), None)
        if candidate is None:
            raise ValidationFailed(
                "Request body must specify space_id",
                details=[
                    {
                        "field": "body.space_id",
                        "rule": "required",
                        "message": (
                            "Caller has no space memberships; an explicit "
                            "space_id is required to create an asset."
                        ),
                    }
                ],
            )
        space_id = candidate

    require_space_access(auth, str(space_id))

    # R1.5: Derived assets must carry a provenance link. We treat the
    # body's ``provenance_source_id`` as the explicit signal. A future
    # ``data_type``-based heuristic (e.g. lifecycle_state='derived') is
    # deferred until Validation_Lambda owns the rules.
    if body.get("derived") is True and not body.get("provenance_source_id"):
        raise MissingProvenance(
            "Derived assets require a provenance_source_id",
            details={
                "field": "provenance_source_id",
                "asset_kind": "derived",
            },
        )

    promoted, jsonb_metadata = _split_promoted_and_metadata(
        body, _DATA_ASSET_PROMOTED_COLUMNS, table="data_asset"
    )

    change_source = _resolve_change_source(event)

    new_id = str(uuid.uuid4())

    with aurora_connection(auth) as conn:
        try:
            with conn.cursor() as cur:
                # Build the INSERT statement deterministically.
                columns = ["id", "space_id", "created_by", "metadata"]
                values: list[Any] = [
                    new_id,
                    str(space_id),
                    auth.user_id,
                    _to_jsonb(jsonb_metadata),
                ]
                for col, val in promoted.items():
                    columns.append(col)
                    values.append(_coerce_for_table("data_asset", col, val))

                placeholders = ", ".join(["%s"] * len(columns))
                cur.execute(
                    f"INSERT INTO data_asset ({', '.join(columns)}) "
                    f"VALUES ({placeholders}) "
                    f"RETURNING *",
                    values,
                )
                row = _row_to_dict(cur)
        except Exception as exc:
            # Translate the unique-constraint violation into the
            # canonical 409 DUPLICATE_ENTITY shape. We pattern-match
            # by index name (most reliable across psycopg versions);
            # the exception type itself differs between psycopg2 /
            # psycopg3 and across drivers, so we look at the message.
            if _is_storage_uri_unique_violation(exc):
                raise DuplicateEntity(
                    "An asset with this storage_uri already exists",
                    details={"storage_uri": storage_uri},
                ) from exc
            raise

        _insert_revision(
            conn,
            entity_type="data_asset",
            entity_id=new_id,
            user_id=auth.user_id,
            change_source=change_source,
            metadata_snapshot=row,
            previous_values=None,
            new_values=None,
        )

    # Soft-warning surface — empty until Task 25.1 adds the
    # similarity check. The wire shape stays stable.
    response_body = dict(row)
    response_body["warnings"] = []

    return _api_response(201, response_body, request_id=request_id)


def _get_asset(
    auth: AuthContext, asset_id: str, *, request_id: str
) -> Mapping[str, Any]:
    """GET /assets/{id} — read one Data_Asset.

    RLS handles structural visibility — a row hidden by Layer 2 is
    invisible at the SQL level and we surface 404 (not 403) per design
    to prevent existence side-channels. Layer 3
    (:func:`check_sensitive_flag`) covers the case where the row is
    structurally visible but the caller lacks the
    ``data_administrator`` privilege required to see sensitive data.
    """
    _require_uuid(asset_id, field="id")

    with aurora_connection(auth) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM data_asset WHERE id = %s",
                (asset_id,),
            )
            row = _row_to_dict(cur)

    if row is None:
        raise NotFound(f"data_asset {asset_id!r} not found")

    check_sensitive_flag(row, auth)
    return _api_response(200, row, request_id=request_id)


def _update_asset(
    auth: AuthContext,
    asset_id: str,
    body: Mapping[str, Any],
    event: Mapping[str, Any],
    *,
    request_id: str,
) -> Mapping[str, Any]:
    """PUT /assets/{id} — update a Data_Asset.

    Computes the field diff against the currently-persisted row,
    issues the UPDATE, and appends a revision row carrying the
    diff. ``validation_status`` is preserved as-is — Validation_
    Lambda owns that field.
    """
    _require_uuid(asset_id, field="id")
    change_source = _resolve_change_source(event)

    promoted, jsonb_metadata_in = _split_promoted_and_metadata(
        body, _DATA_ASSET_PROMOTED_COLUMNS, table="data_asset"
    )

    with aurora_connection(auth) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM data_asset WHERE id = %s FOR UPDATE",
                (asset_id,),
            )
            current = _row_to_dict(cur)
        if current is None:
            raise NotFound(f"data_asset {asset_id!r} not found")

        # Sensitive guard: a non-privileged caller can't update a
        # sensitive asset they couldn't otherwise read.
        check_sensitive_flag(current, auth)

        # Permission: the caller must hold a space role on the asset's
        # space (or be a data administrator).
        require_space_access(auth, str(current["space_id"]))

        previous_values, new_values = _diff_promoted(
            current=current, incoming=promoted
        )

        # The metadata JSONB column is also diffable — but at row-
        # level granularity. If the caller passed any unpromoted
        # fields, we replace the whole metadata blob rather than
        # merging keys, mirroring the design's "PUT replaces" semantics.
        if jsonb_metadata_in:
            new_metadata = jsonb_metadata_in
            if current.get("metadata") != new_metadata:
                previous_values["metadata"] = current.get("metadata") or {}
                new_values["metadata"] = new_metadata
        else:
            new_metadata = current.get("metadata") or {}

        if not new_values:
            # No-op PUT. Return the current row untouched and skip
            # the revision write — empty revisions would pollute the
            # audit log.
            row = current
        else:
            with conn.cursor() as cur:
                set_clauses: list[str] = []
                set_values: list[Any] = []
                for col, value in new_values.items():
                    set_clauses.append(f"{col} = %s")
                    if col == "metadata":
                        set_values.append(_to_jsonb(value))
                    else:
                        set_values.append(
                            _coerce_for_table("data_asset", col, value)
                        )
                # version + updated_at bumped server-side so callers
                # don't have to remember.
                set_clauses.append("version = version + 1")
                set_clauses.append("updated_at = now()")

                cur.execute(
                    f"UPDATE data_asset "
                    f"SET {', '.join(set_clauses)} "
                    f"WHERE id = %s "
                    f"RETURNING *",
                    [*set_values, asset_id],
                )
                row = _row_to_dict(cur)

            _insert_revision(
                conn,
                entity_type="data_asset",
                entity_id=asset_id,
                user_id=auth.user_id,
                change_source=change_source,
                metadata_snapshot=row,
                previous_values=previous_values,
                new_values=new_values,
            )

    return _api_response(200, row, request_id=request_id)


# ---------------------------------------------------------------------------
# Business operations — Metadata_Entity
# ---------------------------------------------------------------------------


def _create_entity(
    auth: AuthContext,
    entity_type: str,
    body: Mapping[str, Any],
    event: Mapping[str, Any],
    *,
    request_id: str,
) -> Mapping[str, Any]:
    """POST /entities/{type} — create a Metadata_Entity."""
    _require_entity_type(entity_type)
    change_source = _resolve_change_source(event)
    promoted_columns = _ENTITY_PROMOTED_COLUMNS[entity_type]
    promoted, jsonb_metadata = _split_promoted_and_metadata(
        body, promoted_columns, table=entity_type
    )

    new_id = str(uuid.uuid4())

    with aurora_connection(auth) as conn:
        with conn.cursor() as cur:
            columns: list[str] = ["id"]
            values: list[Any] = [new_id]
            for col, val in promoted.items():
                columns.append(col)
                values.append(_coerce_for_table(entity_type, col, val))

            if entity_type in _TABLES_WITH_CREATED_BY:
                columns.append("created_by")
                values.append(auth.user_id)

            if entity_type in _TABLES_WITH_METADATA:
                columns.append("metadata")
                values.append(_to_jsonb(jsonb_metadata))

            placeholders = ", ".join(["%s"] * len(columns))
            try:
                cur.execute(
                    f"INSERT INTO {entity_type} ({', '.join(columns)}) "
                    f"VALUES ({placeholders}) "
                    f"RETURNING *",
                    values,
                )
            except Exception as exc:
                # Asset-specific entities reference data_asset_id. RLS
                # will hide a non-visible parent at the SELECT level
                # but a write to a parent the caller can't see fails
                # with a foreign-key violation — translate to 404.
                if _is_fk_violation_data_asset(exc):
                    raise NotFound(
                        f"data_asset referenced by {entity_type} not found "
                        "or not visible to caller"
                    ) from exc
                # Subject/instrument/rig have UNIQUE natural keys that
                # can collide.
                if _is_natural_key_unique_violation(entity_type, exc):
                    raise DuplicateEntity(
                        f"A {entity_type} with this natural key already exists",
                    ) from exc
                raise
            row = _row_to_dict(cur)

        _insert_revision(
            conn,
            entity_type=entity_type,
            entity_id=new_id,
            user_id=auth.user_id,
            change_source=change_source,
            metadata_snapshot=row,
            previous_values=None,
            new_values=None,
        )

    return _api_response(201, row, request_id=request_id)


def _get_entity(
    auth: AuthContext,
    entity_type: str,
    entity_id: str,
    *,
    request_id: str,
) -> Mapping[str, Any]:
    """GET /entities/{type}/{id} — read a Metadata_Entity."""
    _require_entity_type(entity_type)
    _require_uuid(entity_id, field="id")

    with aurora_connection(auth) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM {entity_type} WHERE id = %s",
                (entity_id,),
            )
            row = _row_to_dict(cur)

    if row is None:
        raise NotFound(f"{entity_type} {entity_id!r} not found")

    # Subjects can carry sensitive flags via the parent Data_Asset.
    # The shared check_sensitive_flag handles either ``sensitive_flag``
    # or ``is_sensitive`` — entity rows that don't have either field
    # are treated as not sensitive.
    check_sensitive_flag(row, auth)
    return _api_response(200, row, request_id=request_id)


def _update_entity(
    auth: AuthContext,
    entity_type: str,
    entity_id: str,
    body: Mapping[str, Any],
    event: Mapping[str, Any],
    *,
    request_id: str,
) -> Mapping[str, Any]:
    """PUT /entities/{type}/{id} — update a Metadata_Entity."""
    _require_entity_type(entity_type)
    _require_uuid(entity_id, field="id")
    change_source = _resolve_change_source(event)
    promoted_columns = _ENTITY_PROMOTED_COLUMNS[entity_type]
    promoted, jsonb_metadata_in = _split_promoted_and_metadata(
        body, promoted_columns, table=entity_type
    )

    with aurora_connection(auth) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM {entity_type} WHERE id = %s FOR UPDATE",
                (entity_id,),
            )
            current = _row_to_dict(cur)
        if current is None:
            raise NotFound(f"{entity_type} {entity_id!r} not found")

        check_sensitive_flag(current, auth)

        previous_values, new_values = _diff_promoted(
            current=current, incoming=promoted
        )

        if (
            entity_type in _TABLES_WITH_METADATA
            and jsonb_metadata_in
            and current.get("metadata") != jsonb_metadata_in
        ):
            previous_values["metadata"] = current.get("metadata") or {}
            new_values["metadata"] = jsonb_metadata_in

        if not new_values:
            row = current
        else:
            with conn.cursor() as cur:
                set_clauses: list[str] = []
                set_values: list[Any] = []
                for col, value in new_values.items():
                    set_clauses.append(f"{col} = %s")
                    if col == "metadata":
                        set_values.append(_to_jsonb(value))
                    else:
                        set_values.append(
                            _coerce_for_table(entity_type, col, value)
                        )
                cur.execute(
                    f"UPDATE {entity_type} "
                    f"SET {', '.join(set_clauses)} "
                    f"WHERE id = %s "
                    f"RETURNING *",
                    [*set_values, entity_id],
                )
                row = _row_to_dict(cur)

            _insert_revision(
                conn,
                entity_type=entity_type,
                entity_id=entity_id,
                user_id=auth.user_id,
                change_source=change_source,
                metadata_snapshot=row,
                previous_values=previous_values,
                new_values=new_values,
            )

    return _api_response(200, row, request_id=request_id)


# ---------------------------------------------------------------------------
# Revision write
# ---------------------------------------------------------------------------


def _insert_revision(
    conn: Any,
    *,
    entity_type: str,
    entity_id: str,
    user_id: str,
    change_source: str,
    metadata_snapshot: Mapping[str, Any],
    previous_values: Optional[Mapping[str, Any]],
    new_values: Optional[Mapping[str, Any]],
) -> None:
    """Append exactly one ``entity_revision`` row inside the caller's transaction.

    Computes ``revision_number`` as ``COALESCE(MAX(revision_number), 0) + 1``
    for the (entity_type, entity_id) tuple. The
    ``UNIQUE (entity_type, entity_id, revision_number)`` constraint
    serves as the backstop if two transactions race — the second
    INSERT will fail with a unique-violation, which causes the entire
    update transaction to roll back, exactly the right semantics.

    Idempotency: callers that retry an UPDATE on the same row should
    see a fresh revision_number on each retry — the DB-side MAX +1
    computation guarantees that.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(MAX(revision_number), 0) + 1 "
            "FROM entity_revision "
            "WHERE entity_type = %s AND entity_id = %s",
            (entity_type, entity_id),
        )
        next_number_row = cur.fetchone()
        next_number = int(next_number_row[0]) if next_number_row else 1

        cur.execute(
            "INSERT INTO entity_revision ("
            "  entity_type, entity_id, revision_number, "
            "  user_id, change_source, "
            "  metadata_snapshot, previous_values, new_values"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                entity_type,
                entity_id,
                next_number,
                user_id,
                change_source,
                _to_jsonb(metadata_snapshot),
                _to_jsonb(previous_values) if previous_values else None,
                _to_jsonb(new_values) if new_values else None,
            ),
        )


# ---------------------------------------------------------------------------
# Change-source resolution
# ---------------------------------------------------------------------------


def _resolve_change_source(event: Mapping[str, Any]) -> str:
    """Pick the ``change_source`` token from the request context.

    Header rules (case-insensitive, applied in order):

    1. ``X-Agent-Source: true`` → ``agent`` (Agent UI proxy).
    2. ``X-API-Source:   true`` → ``api`` (programmatic third-party).
    3. Otherwise                → ``manual`` (interactive UI write).

    The values must match enum members of ``change_source_kind``
    (migration 0004); anything else would fail the INSERT.
    """
    headers = event.get("headers") or {}
    if not isinstance(headers, Mapping):
        return "manual"
    lowered = {
        (k.lower() if isinstance(k, str) else str(k)): v
        for k, v in headers.items()
    }
    if _bool_like(lowered.get("x-agent-source")):
        return "agent"
    if _bool_like(lowered.get("x-api-source")):
        return "api"
    return "manual"


def _bool_like(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _split_promoted_and_metadata(
    body: Mapping[str, Any],
    promoted_columns: Iterable[str],
    *,
    table: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a request body into ``promoted`` columns and a ``metadata`` blob.

    Fields whose names appear in the table's promoted-column allow-list
    map to relational columns; everything else lands in the JSONB
    ``metadata`` column for lossless preservation. Skips a small set
    of registry-managed fields (``id``, ``space_id``, ``created_by``,
    ``created_at``, ``updated_at``, ``version``, ``lifecycle_state``,
    ``validation_status``, ``description_vec``,
    ``description_vec_status``, ``embedding``, ``metadata``) so the
    caller can't overwrite them via the body — they're set by the
    handler / DB defaults.
    """
    promoted_set = set(promoted_columns)
    server_managed = {
        "id",
        "space_id",
        "created_by",
        "created_at",
        "updated_at",
        "version",
        "lifecycle_state",
        "validation_status",
        "description_vec",
        "description_vec_status",
        "embedding",
        "metadata",
        # Surface fields that the API client may pass for context but
        # that we never persist directly.
        "warnings",
        "derived",
    }

    promoted: dict[str, Any] = {}
    metadata: dict[str, Any] = {}
    for key, value in body.items():
        if not isinstance(key, str):
            continue
        if key in server_managed:
            continue
        if key in promoted_set:
            promoted[key] = value
        else:
            metadata[key] = value

    # Preserve any explicitly-passed ``metadata`` mapping, merging
    # the unpromoted overflow on top so callers can still control
    # the JSONB blob directly.
    incoming_metadata = body.get("metadata")
    if isinstance(incoming_metadata, Mapping):
        merged = dict(incoming_metadata)
        merged.update(metadata)
        metadata = merged

    return promoted, metadata


def _diff_promoted(
    *,
    current: Mapping[str, Any],
    incoming: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compute (previous_values, new_values) JSON diffs.

    Only fields actually present in ``incoming`` are considered. Other
    columns on the current row are untouched — PUT semantics here are
    "patch the fields you supplied"; the OpenAPI spec marks the body
    as a full DataAsset object but we don't enforce field-by-field
    equivalence with the persisted row.
    """
    previous: dict[str, Any] = {}
    new: dict[str, Any] = {}
    for key, value in incoming.items():
        current_value = current.get(key)
        if _normalize_for_diff(current_value) != _normalize_for_diff(value):
            previous[key] = current_value
            new[key] = value
    return previous, new


def _normalize_for_diff(value: Any) -> Any:
    """Render a value into a comparable, JSON-friendly shape."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        # Compare numerically via float; for our purposes (sub-
        # NUMERIC(10,4) precision) this is round-trip-stable.
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_normalize_for_diff(v) for v in value]
    if isinstance(value, Mapping):
        return {k: _normalize_for_diff(v) for k, v in value.items()}
    return value


def _coerce_for_table(table: str, column: str, value: Any) -> Any:
    """Coerce a Python value into a psycopg-friendly bind parameter.

    Most types (str, int, float, bool, datetime, date) pass through
    psycopg natively. JSONB columns need explicit JSON-string
    serialisation because the column type isn't conveyed by the
    parameterised statement until after the cast.
    """
    if value is None:
        return None
    if column in _JSONB_COLUMNS.get(table, frozenset()):
        return _to_jsonb(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _to_jsonb(value: Any) -> str:
    """JSON-stringify a value for a JSONB bind parameter.

    psycopg can sometimes infer JSONB on a dict/list parameter, but
    the inference depends on driver version + statement-prepare timing.
    Doing the dump explicitly is robust and produces stable output the
    tests can assert on. ``default=_default_serializer`` handles UUIDs,
    Decimals, datetimes, and dates — none of which are JSON-native.
    """
    return json.dumps(value, default=_default_serializer, ensure_ascii=True)


def _default_serializer(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(
        f"object of type {type(value).__name__!r} is not JSON serializable"
    )


def _row_to_dict(cur: Any) -> Optional[dict[str, Any]]:
    """Convert the most recent psycopg cursor result into a dict.

    Uses ``cur.description`` to map column positions to column names.
    Returns ``None`` when the cursor has no rows. JSONB columns are
    materialised by psycopg as native Python dicts/lists; we round-
    trip them through :func:`_normalize_for_diff` so the returned
    payload is JSON-friendly (datetimes / UUIDs / Decimals stringified)
    and round-trip-stable.
    """
    description = getattr(cur, "description", None)
    if description is None:
        return None
    row = cur.fetchone()
    if row is None:
        return None
    columns = [d[0] if not isinstance(d, str) else d for d in description]
    out = {col: row[i] for i, col in enumerate(columns)}
    return {k: _normalize_for_diff(v) for k, v in out.items()}


def _parse_json_body(event: Mapping[str, Any]) -> dict[str, Any]:
    """Pull the JSON body out of the API Gateway event.

    The OpenAPI middleware would have rejected non-JSON bodies on the
    write paths. This helper provides a single source of parsing for
    handlers that don't go through the middleware (the GET endpoints
    short-circuit before the middleware runs).
    """
    raw = event.get("body")
    if raw is None or raw == "":
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationFailed(
                "Request body is not valid JSON",
                details=[
                    {
                        "field": "body",
                        "rule": "json",
                        "message": str(exc),
                    }
                ],
            ) from exc
        if not isinstance(parsed, Mapping):
            raise ValidationFailed(
                "Request body must be a JSON object",
                details=[{"field": "body", "rule": "type"}],
            )
        return dict(parsed)
    raise ValidationFailed(
        f"Unsupported body type {type(raw).__name__!r}",
        details=[{"field": "body", "rule": "type"}],
    )


def _api_response(
    status: int, body: Mapping[str, Any], *, request_id: str
) -> Mapping[str, Any]:
    """Build the API Gateway proxy response shape."""
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "X-Request-Id": request_id,
            # CORS — the SPA at the CloudFront origin needs these on
            # every response. The matching preflight OPTIONS comes from
            # the cors.tf MOCK integrations.
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Authorization,Content-Type,X-Amz-Date,X-Api-Key,X-Amz-Security-Token,X-Agent-Source,X-API-Source",
            "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        },
        "body": json.dumps(body, default=_default_serializer),
    }


def _extract_request_id(event: Mapping[str, Any], context: Any) -> str:
    """Pull the API Gateway request id out of either event shape."""
    rc = event.get("requestContext")
    if isinstance(rc, Mapping):
        rid = rc.get("requestId")
        if isinstance(rid, str) and rid:
            return rid
    rid = getattr(context, "aws_request_id", None)
    if isinstance(rid, str) and rid:
        return rid
    return "unknown"


def _require_path_param(
    path_params: Mapping[str, Any], name: str
) -> str:
    """Return ``path_params[name]`` or raise 404.

    API Gateway routes with template path parameters always populate
    ``pathParameters``; a missing entry indicates a misconfigured
    integration.
    """
    if not isinstance(path_params, Mapping):
        raise NotFound("path parameters not provided")
    raw = path_params.get(name)
    if not isinstance(raw, str) or not raw.strip():
        raise NotFound(f"path parameter {name!r} not provided")
    return raw.strip()


def _require_uuid(value: str, *, field: str) -> None:
    """Validate a path-supplied UUID before opening Aurora.

    Failing fast here saves an Aurora round-trip whose error would be
    indistinguishable from "not found" — since RLS hides invalid IDs
    too, a malformed UUID would otherwise produce a misleading 404.
    """
    try:
        uuid.UUID(value)
    except ValueError as exc:
        raise ValidationFailed(
            f"{field!r} is not a valid UUID",
            details=[
                {
                    "field": field,
                    "rule": "format",
                    "message": str(exc),
                }
            ],
        ) from exc


def _require_entity_type(entity_type: str) -> None:
    """Reject unknown entity types with a structured 400.

    The OpenAPI spec marks ``/entities/{type}`` polymorphic, so the
    middleware does not constrain the path parameter — the closed-set
    check happens here.
    """
    if entity_type not in _VALID_ENTITY_TYPES:
        raise ValidationFailed(
            f"Unknown entity type {entity_type!r}",
            details=[
                {
                    "field": "path.type",
                    "rule": "enum",
                    "message": (
                        f"entity type must be one of "
                        f"{sorted(_VALID_ENTITY_TYPES)!r}"
                    ),
                }
            ],
        )


def _is_storage_uri_unique_violation(exc: Exception) -> bool:
    """True iff ``exc`` is a unique-violation on ``data_asset_storage_uri_unique``.

    psycopg raises ``psycopg.errors.UniqueViolation`` (subclass of
    IntegrityError) — but tests use a fake driver that just raises
    a plain Exception. We pattern-match on the message text + the
    canonical SQLSTATE because both are stable across drivers and
    test doubles.
    """
    return _exc_matches(exc, sqlstate="23505", marker="data_asset_storage_uri_unique")


def _is_natural_key_unique_violation(entity_type: str, exc: Exception) -> bool:
    """True iff ``exc`` is a unique-violation on a natural-key column.

    subject/instrument/rig carry ``UNIQUE (subject_id|instrument_id|rig_id)``
    — colliding inserts produce the same SQLSTATE 23505 backstop we
    use for storage_uri.
    """
    natural_key_markers = {
        "subject": "subject_id",
        "instrument": "instrument_id",
        "rig": "rig_id",
    }
    marker = natural_key_markers.get(entity_type)
    if marker is None:
        return False
    return _exc_matches(exc, sqlstate="23505", marker=marker)


def _is_fk_violation_data_asset(exc: Exception) -> bool:
    """True iff ``exc`` is a foreign-key violation on a data_asset_id column."""
    return _exc_matches(exc, sqlstate="23503", marker="data_asset_id")


def _exc_matches(exc: Exception, *, sqlstate: str, marker: str) -> bool:
    """Match a Postgres error by SQLSTATE OR substring of the message.

    psycopg3 exposes ``exc.sqlstate`` directly, but our fake test
    driver does not — supporting both keeps the unit tests free of
    psycopg-specific knowledge.
    """
    candidate = getattr(exc, "sqlstate", None)
    if isinstance(candidate, str) and candidate == sqlstate:
        return True
    diag = getattr(exc, "diag", None)
    if diag is not None:
        diag_state = getattr(diag, "sqlstate", None)
        if isinstance(diag_state, str) and diag_state == sqlstate:
            return True
    text = str(exc).lower()
    return marker.lower() in text


__all__ = ("handler",)

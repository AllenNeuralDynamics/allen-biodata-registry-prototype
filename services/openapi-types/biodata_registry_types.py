"""
Allen BioData Registry PoC — registry-governance Pydantic models.

Authoritative Pydantic v2 source for the *non*-aind-data-schema models
that the OpenAPI spec references. The biological metadata models
(Subject, Instrument, Rig, Procedures, Session, Acquisition, Processing,
QualityControl, DataDescription, DataAsset) live in the upstream
``aind-data-schema`` library and are exported by
``scripts/export_schemas.py`` directly via ``model.model_json_schema()``.

The models in this file cover the registry's *governance* surface,
which is registry-specific and does not have a counterpart in
aind-data-schema:

* :class:`Organization` — top-level tenant (R9.1).
* :class:`Space` — sub-tenant within an organization (R9.1).
* :class:`Role` — closed enum of role kinds (R9.2, R9.7).
* :class:`SharingGrant` — cross-org sharing entry (R9.5, R9.6).
* :class:`EntityRevision` — immutable revision history record
  (R6.1, R6.2, R23.3).
* :class:`LifecycleTransition` — single state-machine transition row
  (R27.6).
* :class:`DuplicateFlag` — admin-reviewable duplicate signal (R3.4,
  R26.2).
* :class:`ChangeSourceKind` — provenance enum on each revision
  (R6.1, R6.2).
* :class:`LifecycleState`, :class:`ValidationStatus` — DB enums
  surfaced over the API (R27.1).

Mirrors the Aurora DDL in ``migrations/0001_governance.sql`` and
``migrations/0004_revisions_lifecycle_duplicates.sql`` so what's in
the API is what's in the database — drift here would mean drift
between the OpenAPI spec and the source of truth.

Validates: design.md §External Interfaces.API Gateway REST (OpenAPI
authoring), §Data Models.Aurora.Governance tables.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ---------------------------------------------------------------------------
# Enums (DB-backed)
# ---------------------------------------------------------------------------


class Role(str, enum.Enum):
    """Role kinds (mirrors Aurora ``role_kind`` enum, R9.2, R9.7)."""

    ORG_ADMIN = "org_admin"
    SPACE_ADMIN = "space_admin"
    DATA_ADMINISTRATOR = "data_administrator"
    VIEWER = "viewer"


class LifecycleState(str, enum.Enum):
    """Data_Asset lifecycle state (Aurora ``lifecycle_state`` enum, R27.1)."""

    DRAFT = "draft"
    REGISTERED = "registered"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class ValidationStatus(str, enum.Enum):
    """Validation status (Aurora ``validation_status`` enum, R4)."""

    VALID = "valid"
    INVALID = "invalid"
    UNVALIDATED = "unvalidated"
    SCHEMA_DEPRECATED = "schema-deprecated"


class ChangeSourceKind(str, enum.Enum):
    """Provenance of an entity revision (Aurora ``change_source_kind``, R6)."""

    MANUAL = "manual"
    AGENT = "agent"
    API = "api"
    MERGE = "merge"
    ETL = "ETL"


class EntityType(str, enum.Enum):
    """Closed set of entity types the revision history tracks.

    Kept in lockstep with the shared and asset-specific entity tables
    in design.md §Data Models.Aurora.
    """

    DATA_ASSET = "data_asset"
    SUBJECT = "subject"
    INSTRUMENT = "instrument"
    RIG = "rig"
    PROCEDURES = "procedures"
    SESSION = "session"
    ACQUISITION = "acquisition"
    PROCESSING = "processing"
    QUALITY_CONTROL = "quality_control"
    DATA_DESCRIPTION = "data_description"


# ---------------------------------------------------------------------------
# Governance models
# ---------------------------------------------------------------------------


class _StrictModel(BaseModel):
    """Shared base — forbid extra fields so OpenAPI clients fail fast on drift."""

    model_config = ConfigDict(
        extra="forbid",
        # Ensure UUID/datetime/date are exported as strings in JSON
        # Schema instead of complex objects, matching the wire shape
        # the OpenAPI clients expect.
        json_schema_serialization_defaults_required=True,
    )


class Organization(_StrictModel):
    """Top-level tenant (R9.1).

    Mirrors ``organization`` table. ``name`` is the unique stable
    identifier; ``display_name`` is the human-friendly label surfaced
    in the UI. We split the two so renaming for display does not
    cascade into FK churn.
    """

    id: UUID = Field(
        ...,
        description="Server-generated UUID. Immutable.",
    )
    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
        description=(
            "Stable lowercase identifier (slug). Unique across the registry. "
            "Used in URLs and SNS topic names."
        ),
    )
    display_name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Human-readable organization name shown in the UI.",
    )
    created_at: datetime = Field(
        ...,
        description="Creation timestamp (UTC).",
    )


class Space(_StrictModel):
    """Sub-tenant within an organization (R9.1).

    Mirrors ``space`` table. ``parent_space_id`` is reserved for the
    optional nested-space pattern; left as ``Optional`` to match the
    DDL where the column is nullable for top-level spaces. The
    ``(org_id, name)`` uniqueness invariant is enforced by Aurora;
    the API surface only validates shape here.
    """

    id: UUID = Field(..., description="Server-generated UUID. Immutable.")
    org_id: UUID = Field(..., description="Parent organization id.")
    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
        description="Lowercase slug, unique within the organization.",
    )
    display_name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Human-readable space name.",
    )
    parent_space_id: Optional[UUID] = Field(
        default=None,
        description=(
            "Parent space when nesting is enabled; ``null`` for top-level "
            "spaces. Reserved for future use — the PoC does not nest."
        ),
    )
    created_at: datetime = Field(..., description="Creation timestamp (UTC).")


class SharingGrant(_StrictModel):
    """Cross-org sharing entry (R9.5, R9.6).

    Mirrors ``sharing_grant`` table. The grantee is exactly one of
    ``grantee_org_id``, ``grantee_space_id``, or ``grantee_user_id``;
    the ``CHECK`` constraint at the DB layer enforces "at least one"
    but the API model is stricter — exactly one — so clients cannot
    construct ambiguous grants.
    """

    id: UUID = Field(..., description="Server-generated UUID. Immutable.")
    granter_org_id: UUID = Field(
        ...,
        description="The org granting access. Always set.",
    )
    grantee_org_id: Optional[UUID] = Field(
        default=None,
        description="When the grantee is an entire organization.",
    )
    grantee_space_id: Optional[UUID] = Field(
        default=None,
        description="When the grantee is a specific space.",
    )
    grantee_user_id: Optional[UUID] = Field(
        default=None,
        description="When the grantee is a single user (admin convenience).",
    )
    role: Role = Field(
        ...,
        description="Role conferred on the grantee.",
    )
    granted_at: datetime = Field(..., description="When the grant was created.")
    expires_at: Optional[datetime] = Field(
        default=None,
        description="Optional expiration; ``null`` means the grant is open-ended.",
    )
    created_by: UUID = Field(
        ...,
        description="User id of the org_admin who created the grant.",
    )

    @model_validator(mode="after")
    def _exactly_one_grantee(self) -> "SharingGrant":
        """Reject grants that target zero or more than one principal."""
        principals = [
            self.grantee_org_id,
            self.grantee_space_id,
            self.grantee_user_id,
        ]
        set_count = sum(p is not None for p in principals)
        if set_count != 1:
            raise ValueError(
                "exactly one of grantee_org_id, grantee_space_id, "
                "grantee_user_id must be set"
            )
        return self


class EntityRevision(_StrictModel):
    """Immutable revision history row (R6.1, R6.2, R23.3).

    Mirrors ``entity_revision`` table. Per design.md the underlying
    table has ``REVOKE UPDATE, DELETE`` so this is, by construction,
    append-only — the OpenAPI surface exposes only ``GET`` operations
    on revisions (see Revisions_Lambda).
    """

    id: int = Field(
        ...,
        ge=1,
        description="BIGSERIAL revision id. Monotonic across the table.",
    )
    entity_type: EntityType = Field(
        ...,
        description="Which table the revision describes.",
    )
    entity_id: UUID = Field(..., description="Primary key of the revisioned row.")
    revision_number: int = Field(
        ...,
        ge=1,
        description=(
            "Per-entity revision counter. ``UNIQUE (entity_type, entity_id, "
            "revision_number)`` is enforced at the DB."
        ),
    )
    user_id: UUID = Field(..., description="User who triggered the revision.")
    timestamp: datetime = Field(..., description="UTC timestamp.")
    change_source: ChangeSourceKind = Field(
        ...,
        description="What system action produced this revision.",
    )
    metadata_snapshot: dict = Field(
        ...,
        description="Full JSONB snapshot of the entity at this revision.",
    )
    previous_values: Optional[dict] = Field(
        default=None,
        description="Diff payload — fields that changed (old values).",
    )
    new_values: Optional[dict] = Field(
        default=None,
        description="Diff payload — fields that changed (new values).",
    )


class LifecycleTransition(_StrictModel):
    """A single Data_Asset state-machine transition (R27.6).

    Mirrors ``lifecycle_transition`` table. One row per successful
    transition; rejected transitions never write here (they surface
    as :class:`InvalidStateTransition` errors instead).
    """

    id: int = Field(..., ge=1, description="BIGSERIAL transition id.")
    data_asset_id: UUID = Field(
        ...,
        description="Asset whose lifecycle changed.",
    )
    user_id: UUID = Field(..., description="User who initiated the transition.")
    timestamp: datetime = Field(..., description="UTC timestamp.")
    previous_state: LifecycleState = Field(..., description="State before.")
    new_state: LifecycleState = Field(..., description="State after.")


class DuplicateFlag(_StrictModel):
    """Admin-reviewable duplicate signal (R3.4, R26.2).

    Mirrors ``duplicate_flag`` table. ``similarity_score`` is bounded
    [0, 1]; rows are created either by the synchronous duplicate
    check on registration or by the EventBridge-scheduled background
    scan (R3.5).
    """

    id: UUID = Field(..., description="Server-generated UUID.")
    entity_type: EntityType = Field(
        ...,
        description="Which entity table the flagged pair lives in.",
    )
    entity_a_id: UUID = Field(..., description="One side of the candidate pair.")
    entity_b_id: UUID = Field(..., description="Other side of the candidate pair.")
    similarity_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description=(
            "Similarity in [0, 1]. Computed by exact URI match (1.0), "
            "trigram similarity, or pgvector cosine — the source is "
            "captured in the ``flag_meta`` JSONB on the underlying row."
        ),
    )
    flagged_at: datetime = Field(..., description="When the flag was created.")
    dismissed: bool = Field(
        default=False,
        description="True after an admin clicks 'Dismiss' (R26.5).",
    )
    dismissed_by: Optional[UUID] = Field(
        default=None,
        description="User who dismissed; null until dismissal.",
    )
    dismissed_at: Optional[datetime] = Field(
        default=None,
        description="Dismissal timestamp; null until dismissal.",
    )


# ---------------------------------------------------------------------------
# Error response shape (Property 14, R30)
# ---------------------------------------------------------------------------


class ErrorResponse(_StrictModel):
    """Canonical error body emitted by every Lambda (Property 14, R30).

    Mirrors the structure produced by
    ``biodata_registry_shared.errors.make_error_response``. We export
    this here (not in the shared layer) because the Layer ships
    inside Lambdas, while the OpenAPI spec is a build-time artifact
    consumed by the client generator.
    """

    code: str = Field(
        ...,
        description=(
            "Machine-readable error code from the closed enumerated set. "
            "See design.md §Error Handling.Error Code Mapping."
        ),
    )
    message: str = Field(
        ...,
        min_length=1,
        description="Human-readable summary; never empty.",
    )
    details: dict | list = Field(
        ...,
        description=(
            "Per-code structured payload. ``VALIDATION_FAILED`` returns a "
            "list of ``{field, rule}`` entries; ``INVALID_STATE_TRANSITION`` "
            "returns ``{current_state, allowed_transitions}``; etc. Always "
            "present (never null); empty ``{}`` when the code carries no "
            "structured details (e.g. ``SENSITIVE_ACCESS_DENIED``)."
        ),
    )
    request_id: str = Field(
        ...,
        description=(
            "API Gateway / Lambda request id for log correlation. May be "
            "empty when the response is shaped outside a request context "
            "(testing) but the field is always present."
        ),
    )
    timestamp: datetime = Field(
        ...,
        description="UTC timestamp; ISO 8601 with millisecond precision.",
    )


# ---------------------------------------------------------------------------
# Soft-warning duplicate detection response (R3, R26)
# ---------------------------------------------------------------------------


class DuplicateWarning(_StrictModel):
    """Soft-warning entry attached to a 201 Created response (R3, R26).

    Per design.md §Components.Duplicates_Lambda the only 409 path is
    the database-level unique constraint on ``data_asset.storage_uri``;
    similarity-based candidates are advisory and surface here as
    ``warnings: [...]`` on the success response.
    """

    type: str = Field(
        default="likely_duplicate",
        pattern=r"^likely_duplicate$",
        description=(
            "Always ``likely_duplicate`` for now; the field is reserved "
            "for future warning categories."
        ),
    )
    existing_asset_id: UUID = Field(
        ...,
        description="Id of the existing asset that triggered the warning.",
    )
    similarity_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Similarity in [0, 1].",
    )
    reason: str = Field(
        ...,
        description=(
            "Short explanation, e.g. ``vector_cosine`` or ``trigram_name``, "
            "for UI display."
        ),
    )


class Warnings(_StrictModel):
    """Container for the ``warnings`` array on a 201 response."""

    warnings: list[DuplicateWarning] = Field(
        default_factory=list,
        description=(
            "Zero-or-more soft duplicate warnings. An empty array means "
            "no candidates exceeded the configured warn threshold."
        ),
    )


# ---------------------------------------------------------------------------
# The list of registry-governance models the export script writes out.
# ---------------------------------------------------------------------------


REGISTRY_MODELS: tuple[type[BaseModel], ...] = (
    Organization,
    Space,
    SharingGrant,
    EntityRevision,
    LifecycleTransition,
    DuplicateFlag,
    ErrorResponse,
    Warnings,
    DuplicateWarning,
)
"""All registry-only Pydantic models exported by ``export_schemas.py``.

:class:`Role`, :class:`LifecycleState`, :class:`ValidationStatus`,
:class:`ChangeSourceKind`, :class:`EntityType` are exported as
standalone JSON Schema enums by the script — they live in
``REGISTRY_ENUMS`` below.
"""


REGISTRY_ENUMS: tuple[type[enum.Enum], ...] = (
    Role,
    LifecycleState,
    ValidationStatus,
    ChangeSourceKind,
    EntityType,
)
"""Enums emitted as standalone JSON Schema files for ``$ref`` use."""


__all__ = (
    "ChangeSourceKind",
    "DuplicateFlag",
    "DuplicateWarning",
    "EntityRevision",
    "EntityType",
    "ErrorResponse",
    "LifecycleState",
    "LifecycleTransition",
    "Organization",
    "REGISTRY_ENUMS",
    "REGISTRY_MODELS",
    "Role",
    "SharingGrant",
    "Space",
    "ValidationStatus",
    "Warnings",
)

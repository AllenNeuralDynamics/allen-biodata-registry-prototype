-- =============================================================================
-- Migration: 0005_collections_schemas.sql
-- Purpose:   Curated groupings of Data_Assets (`collection` + junctions +
--            hierarchy) and the registry-managed schema catalog
--            (`schema_definition`). Also wires the long-pending
--            `data_asset.schema_id` foreign key from 0002 to
--            `schema_definition(id)`, now that the target table exists.
--
-- Spec:      .kiro/specs/allen-biodata-registry-poc
-- Task:      7.5 — Create migration 0005_collections_schemas.sql
-- Validates: R4.4, R5.1, R12.1, R12.2, R12.3, R12.4
-- Design:    §Data Models.Aurora.Collections, schemas
--            §Components.9. Collections_Lambda (cycle-detection contract)
--
-- Idempotency:
--   * Tables, indexes, and unique constraints use IF NOT EXISTS.
--   * Enums are wrapped in DO blocks guarded by EXCEPTION
--     WHEN duplicate_object so re-runs are no-ops.
--   * Functions use CREATE OR REPLACE.
--   * The cycle-check trigger is re-installed via DROP TRIGGER IF EXISTS
--     followed by CREATE TRIGGER (Postgres lacks CREATE TRIGGER IF NOT
--     EXISTS); both statements are safe on first and subsequent runs.
--   * The cross-migration FK added to `data_asset.schema_id` is wrapped
--     in a DO block that probes `pg_constraint` so the ALTER is skipped
--     on re-runs.
--   * Re-running this migration after a successful first run is a no-op.
--
-- Ordering:
--   * The migration runner (Task 8.1) applies *.sql files in lexical order,
--     so 0001..0004 are applied first. 0005 depends on:
--       - `space`, `organization`, `app_user` (0001)
--       - `lifecycle_state` enum and `data_asset` (0002)
--
-- Deviations from design.md (called out per the README authoring rules):
--   * `collection.space_id` instead of `collection.org_id`. The task brief
--     scopes Collections to a Space (matching the rest of the system's RLS
--     boundary on `space_id`); design.md sketched org-level scoping which
--     would have required a separate RLS path. UNIQUE (space_id, name)
--     replaces design.md's implicit per-org uniqueness.
--   * `collection` carries `lifecycle_state` (reusing the enum from 0002),
--     `display_name`, `description`, `created_by`, `updated_at`, and a
--     `version` integer for optimistic concurrency — matching the shape
--     of `data_asset` so Collections_Lambda can reuse the same revision /
--     publish patterns.
--   * `collection_asset` uses ON DELETE CASCADE on BOTH sides (vs design's
--     RESTRICT on `data_asset_id`). Rationale: a collection is a curated
--     pointer set, not the system of record for the asset; deleting an
--     asset should remove its (now-dangling) collection memberships. The
--     asset deletion path itself remains gated by RLS / role checks at
--     the API layer.
--   * `collection_hierarchy` adds a synthetic `id UUID PRIMARY KEY` and
--     `added_by` / `added_at` audit columns, with UNIQUE (parent_id,
--     child_id) replacing the composite PK in design.md. Both FKs are
--     ON DELETE CASCADE so deleting either endpoint cleans up the link
--     automatically. The row-level CHECK (parent_id <> child_id) blocks
--     direct self-loops; transitive cycles (A->B->C->A) are caught by
--     `detect_collection_cycle()` invoked from a BEFORE INSERT trigger
--     defined in this file (R12.3, design.md §Components.9).
--   * `schema_definition` follows the task brief: a discriminator enum
--     `schema_kind` ('biodata_default','custom'), `is_active boolean`,
--     `deprecated_at timestamptz`, `definition_jsonschema jsonb`,
--     `owner_org_id` (NULL for biodata_default), and UNIQUE (name,
--     version). Design.md's `is_current` / `deprecation_threshold` /
--     `json_schema` are renamed accordingly.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- Enum: schema_kind
-- -----------------------------------------------------------------------------
-- Discriminates the two flavors of schema known to the registry:
--   * 'biodata_default' — the canonical aind-data-schema bundle. Exactly
--     one chain of `biodata_default` rows exists registry-wide; their
--     `owner_org_id` is NULL. R4.1.
--   * 'custom'          — an Organization-registered Custom_Schema (R4.4).
--     Validation runs additively against both biodata + custom (R4.5).
DO $$
BEGIN
  CREATE TYPE schema_kind AS ENUM (
    'biodata_default',
    'custom'
  );
EXCEPTION
  WHEN duplicate_object THEN
    NULL;
END;
$$;

COMMENT ON TYPE schema_kind IS
  'Discriminator for schema_definition rows. ''biodata_default'' is the registry-wide aind-data-schema bundle (R4.1, owner_org_id IS NULL). ''custom'' is an Organization-registered JSON Schema (R4.4).';


-- =============================================================================
-- Table: collection
-- =============================================================================
-- A curated grouping of Data_Assets (and child collections), used to
-- bundle data for papers and citations (R12.1, R12.4). Lives inside a
-- Space — the same governance boundary as Data_Asset — so RLS scoping
-- composes the same way.
--
-- The `lifecycle_state` column reuses the enum from 0002. Collections
-- intentionally share the asset state machine vocabulary so the
-- Lifecycle_Lambda implementation (Task 23) can reuse its transition
-- helpers when the customer publishes a curated collection.
CREATE TABLE IF NOT EXISTS collection (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  -- Governance scope. RESTRICT prevents accidentally deleting a space that
  -- still owns collections; mirrors data_asset.space_id ON DELETE RESTRICT.
  space_id        UUID NOT NULL REFERENCES space(id) ON DELETE RESTRICT,

  -- Display & uniqueness fields. citext for case-insensitive uniqueness
  -- inside a space (R17 search-friendliness, matching data_asset.name).
  name            CITEXT NOT NULL,
  display_name    TEXT,
  description     TEXT,

  -- DOI for citation. NULL until Collections_Lambda's PUT /collections/{id}/doi
  -- (R12.4, R13.3) populates it post-publish.
  doi             TEXT,

  -- Reuses the lifecycle_state enum created in 0002. Default 'draft' so
  -- Collections, like Data_Assets, must be explicitly published.
  lifecycle_state lifecycle_state NOT NULL DEFAULT 'draft',

  -- Audit. created_by FK into app_user. RESTRICT so deleting a user does
  -- not orphan their writes; mirrors data_asset.created_by.
  created_by      UUID NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

  -- Optimistic concurrency. Bumped by Collections_Lambda on every PUT.
  version         INTEGER NOT NULL DEFAULT 1,

  -- A collection's name is unique within its owning Space.
  CONSTRAINT collection_space_id_name_unique UNIQUE (space_id, name),

  -- Sanity: updated_at >= created_at.
  CONSTRAINT collection_updated_after_created
    CHECK (updated_at >= created_at),

  -- Sanity: version is positive.
  CONSTRAINT collection_version_positive
    CHECK (version >= 1)
);

CREATE INDEX IF NOT EXISTS collection_space_id_idx
  ON collection (space_id);

CREATE INDEX IF NOT EXISTS collection_created_by_idx
  ON collection (created_by);

CREATE INDEX IF NOT EXISTS collection_lifecycle_state_idx
  ON collection (lifecycle_state);

-- Partial index speeds up DOI lookups for citation tooling.
CREATE INDEX IF NOT EXISTS collection_doi_idx
  ON collection (doi)
  WHERE doi IS NOT NULL;

COMMENT ON TABLE  collection                  IS 'Curated grouping of Data_Assets for papers, datasets, citations. R12.1, R12.4.';
COMMENT ON COLUMN collection.id               IS 'UUID primary key.';
COMMENT ON COLUMN collection.space_id         IS 'Owning Space. RLS scope (R10.1, R12.6). ON DELETE RESTRICT.';
COMMENT ON COLUMN collection.name             IS 'Case-insensitive collection name. UNIQUE within space_id.';
COMMENT ON COLUMN collection.display_name     IS 'Human-friendly display name shown in the UI.';
COMMENT ON COLUMN collection.description      IS 'Optional long-form description of the collection''s purpose.';
COMMENT ON COLUMN collection.doi              IS 'DOI for citation. Populated by PUT /collections/{id}/doi after publish (R12.4, R13.3).';
COMMENT ON COLUMN collection.lifecycle_state  IS 'Lifecycle state, reusing the enum from 0002. Default ''draft''. R27.1.';
COMMENT ON COLUMN collection.created_by       IS 'app_user that created the collection.';
COMMENT ON COLUMN collection.created_at       IS 'Record creation timestamp (UTC).';
COMMENT ON COLUMN collection.updated_at       IS 'Last modification timestamp (UTC).';
COMMENT ON COLUMN collection.version          IS 'Optimistic-concurrency revision counter. Bumped by Collections_Lambda on every PUT.';


-- =============================================================================
-- Table: collection_asset
-- =============================================================================
-- Junction linking Collections to Data_Assets by reference (R12.2). The
-- collection never owns the underlying asset; it just points at it. ON
-- DELETE CASCADE on both sides keeps the junction tidy when either
-- endpoint is removed (the asset-deletion path itself is gated by RLS /
-- role checks at the API layer, not by this junction).
CREATE TABLE IF NOT EXISTS collection_asset (
  collection_id UUID NOT NULL REFERENCES collection(id) ON DELETE CASCADE,
  data_asset_id UUID NOT NULL REFERENCES data_asset(id) ON DELETE CASCADE,
  added_by      UUID NOT NULL REFERENCES app_user(id)   ON DELETE RESTRICT,
  added_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (collection_id, data_asset_id)
);

-- Reverse-lookup index: "which collections include data_asset X?"
-- The composite PK already covers the (collection_id, ...) direction.
CREATE INDEX IF NOT EXISTS collection_asset_data_asset_id_idx
  ON collection_asset (data_asset_id);

CREATE INDEX IF NOT EXISTS collection_asset_added_by_idx
  ON collection_asset (added_by);

COMMENT ON TABLE  collection_asset               IS 'Reference link from a Collection to a Data_Asset. R12.2 (no copy of underlying data).';
COMMENT ON COLUMN collection_asset.collection_id IS 'FK -> collection(id). ON DELETE CASCADE.';
COMMENT ON COLUMN collection_asset.data_asset_id IS 'FK -> data_asset(id). ON DELETE CASCADE — junction follows the asset.';
COMMENT ON COLUMN collection_asset.added_by      IS 'app_user that added the asset to the collection.';
COMMENT ON COLUMN collection_asset.added_at      IS 'When the asset was linked into the collection (UTC).';


-- =============================================================================
-- Table: collection_hierarchy
-- =============================================================================
-- Parent->child edges between Collections, modeling nested datasets
-- (R12.3). The semantics are a directed acyclic graph (DAG): a single
-- collection may have multiple parents and multiple children, but no
-- cycles are permitted.
--
-- Two layers of cycle protection:
--   1. Row-level CHECK (parent_id <> child_id) blocks direct self-loops.
--   2. The detect_collection_cycle() function (defined below) is invoked
--      by a BEFORE INSERT trigger on this table to reject transitive
--      cycles (A -> B -> C, then attempting C -> A) before they land.
--
-- The Collections_Lambda also runs the same recursive-CTE check at the
-- API layer so it can return a structured INVALID_HIERARCHY error with
-- a `cycle_path` payload (design.md §Components.9). The trigger is the
-- defense-in-depth backstop in case any other writer reaches the table
-- (data seeders, ad-hoc psql) bypasses the API.
CREATE TABLE IF NOT EXISTS collection_hierarchy (
  id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  parent_id UUID NOT NULL REFERENCES collection(id) ON DELETE CASCADE,
  child_id  UUID NOT NULL REFERENCES collection(id) ON DELETE CASCADE,
  added_by  UUID NOT NULL REFERENCES app_user(id)   ON DELETE RESTRICT,
  added_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT collection_hierarchy_no_self_loop
    CHECK (parent_id <> child_id),

  CONSTRAINT collection_hierarchy_parent_child_unique
    UNIQUE (parent_id, child_id)
);

CREATE INDEX IF NOT EXISTS collection_hierarchy_parent_id_idx
  ON collection_hierarchy (parent_id);

CREATE INDEX IF NOT EXISTS collection_hierarchy_child_id_idx
  ON collection_hierarchy (child_id);

CREATE INDEX IF NOT EXISTS collection_hierarchy_added_by_idx
  ON collection_hierarchy (added_by);

COMMENT ON TABLE  collection_hierarchy           IS 'Parent->child DAG edges between Collections. R12.3. Direct self-loops blocked by CHECK; transitive cycles blocked by detect_collection_cycle() trigger.';
COMMENT ON COLUMN collection_hierarchy.id        IS 'UUID primary key.';
COMMENT ON COLUMN collection_hierarchy.parent_id IS 'FK -> collection(id). ON DELETE CASCADE.';
COMMENT ON COLUMN collection_hierarchy.child_id  IS 'FK -> collection(id). ON DELETE CASCADE.';
COMMENT ON COLUMN collection_hierarchy.added_by  IS 'app_user that nested the child under the parent.';
COMMENT ON COLUMN collection_hierarchy.added_at  IS 'When the parent->child edge was created (UTC).';



-- =============================================================================
-- Function: detect_collection_cycle(p_parent_id UUID, p_child_id UUID)
-- =============================================================================
-- Returns TRUE if inserting the proposed parent->child edge into
-- collection_hierarchy would close a cycle in the Collection DAG.
--
-- Algorithm: a recursive CTE walks the existing hierarchy *forward* from
-- the proposed child_id (i.e., enumerates every collection reachable from
-- child_id by following parent->child edges). If any of those reachable
-- collections is the proposed parent_id, then adding parent_id -> child_id
-- would close a cycle (parent_id -> child_id -> ... -> parent_id) and the
-- function returns TRUE. Otherwise FALSE.
--
-- The function also returns TRUE for the trivial self-loop case
-- (p_parent_id = p_child_id) — that case is also blocked by the
-- CHECK (parent_id <> child_id) row constraint, but checking it here
-- means the function is a complete cycle oracle on its own and can be
-- reused by the Collections_Lambda recursive-CTE pre-check (design.md
-- §Components.9) without needing a separate self-loop guard.
--
-- Performance: the recursive CTE is bounded by the depth of the DAG.
-- Collections in the registry are expected to be shallow (paper-level
-- bundles), so this stays O(depth + branching) per insert.
--
-- Idempotency: CREATE OR REPLACE FUNCTION is the canonical idempotent
-- pattern for Postgres functions; signature changes will fail loudly
-- because the parameter types are part of the identity.
CREATE OR REPLACE FUNCTION detect_collection_cycle(
  p_parent_id UUID,
  p_child_id  UUID
)
RETURNS BOOLEAN
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
  cycle_found BOOLEAN;
BEGIN
  -- Trivial self-loop: parent_id = child_id is a cycle of length 1.
  IF p_parent_id = p_child_id THEN
    RETURN TRUE;
  END IF;

  -- Walk forward from p_child_id through every existing parent->child
  -- edge. If we encounter p_parent_id during the walk, the proposed edge
  -- would close a cycle.
  WITH RECURSIVE descendants(collection_id) AS (
    -- Anchor: the proposed child itself.
    SELECT p_child_id
    UNION
    -- Step: every collection reachable from a known descendant.
    SELECT ch.child_id
    FROM   collection_hierarchy ch
    JOIN   descendants d ON d.collection_id = ch.parent_id
  )
  SELECT EXISTS (
    SELECT 1 FROM descendants WHERE collection_id = p_parent_id
  )
  INTO cycle_found;

  RETURN cycle_found;
END;
$$;

COMMENT ON FUNCTION detect_collection_cycle(UUID, UUID) IS
  'Returns TRUE iff inserting the proposed (parent_id, child_id) edge into collection_hierarchy would create a cycle. Used by both the BEFORE INSERT trigger (defense-in-depth) and the Collections_Lambda API-layer pre-check (R12.3, design.md §Components.9).';


-- =============================================================================
-- Trigger: collection_hierarchy_no_cycle_trg
-- =============================================================================
-- Fires BEFORE INSERT on collection_hierarchy and raises an exception if
-- detect_collection_cycle() reports that the new row would close a cycle.
-- This is the database-side backstop; the API path also pre-checks via
-- the same function, but a non-API writer (seeders, ad-hoc psql) cannot
-- bypass this trigger.
--
-- Idempotency: CREATE TRIGGER has no IF NOT EXISTS form, so we DROP it
-- first and then CREATE. The function used by the trigger is created
-- via CREATE OR REPLACE above, so a brief moment between DROP and CREATE
-- TRIGGER is the only window during which inserts would be unguarded —
-- and migrations run inside a transaction, so even that window is not
-- visible to other sessions.
CREATE OR REPLACE FUNCTION collection_hierarchy_cycle_check_trg_fn()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  IF detect_collection_cycle(NEW.parent_id, NEW.child_id) THEN
    RAISE EXCEPTION
      'collection_hierarchy cycle: inserting (parent=%, child=%) would create a cycle',
      NEW.parent_id, NEW.child_id
      USING ERRCODE = 'check_violation',
            HINT    = 'Choose a different parent or remove an existing edge before re-inserting.';
  END IF;
  RETURN NEW;
END;
$$;

COMMENT ON FUNCTION collection_hierarchy_cycle_check_trg_fn() IS
  'Trigger function for collection_hierarchy_no_cycle_trg. Calls detect_collection_cycle() and raises check_violation if a cycle would be created.';

DROP TRIGGER IF EXISTS collection_hierarchy_no_cycle_trg ON collection_hierarchy;

CREATE TRIGGER collection_hierarchy_no_cycle_trg
  BEFORE INSERT ON collection_hierarchy
  FOR EACH ROW
  EXECUTE FUNCTION collection_hierarchy_cycle_check_trg_fn();

COMMENT ON TRIGGER collection_hierarchy_no_cycle_trg ON collection_hierarchy IS
  'BEFORE INSERT cycle-detection backstop for collection_hierarchy. Defends against non-API writers (seeders, ad-hoc psql). R12.3.';


-- =============================================================================
-- Table: schema_definition
-- =============================================================================
-- Registry-managed catalog of JSON schemas used by Validation_Lambda
-- (R4, R5). Two flavors:
--   * 'biodata_default' — the canonical aind-data-schema bundle. Exactly
--     one chain of these exists registry-wide (versioned by `version`);
--     `owner_org_id` is NULL.
--   * 'custom'          — Organization-registered Custom_Schemas (R4.4).
--     `owner_org_id` is the owning Organization.
--
-- Versions are append-only:
--   * Validation_Lambda's POST /schemas/{id}/versions (Task 21.1) inserts
--     a new row with the same name, a new version string, and is_active
--     = true. The deprecation_threshold semantic from design.md is
--     replaced by an explicit `deprecated_at TIMESTAMPTZ NULL` column;
--     when set, the row's `is_active` should also be flipped to false.
--   * Revalidation_Lambda (Task 22.2) flags Data_Assets whose only
--     validating row has `deprecated_at IS NOT NULL` as
--     'schema-deprecated' (R5.4).
CREATE TABLE IF NOT EXISTS schema_definition (
  id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  -- Logical name of the schema. Plain text (NOT citext) because schema
  -- names are programmatic identifiers (e.g. 'subject', 'instrument',
  -- 'allen-cohort-v3') and case is meaningful.
  name                   TEXT NOT NULL,

  -- Version string. Free-form text rather than a SemVer struct because
  -- the customer may use SemVer, calendar versioning, or aind-data-schema
  -- release tags interchangeably. Validation_Lambda treats this as
  -- opaque except for caching keys.
  version                TEXT NOT NULL,

  -- Discriminator (see schema_kind enum above).
  schema_kind            schema_kind NOT NULL,

  -- Active flag. False once Validation_Lambda retires a version. The
  -- Schema_Cache (R20.4) keys on the active row only.
  is_active              BOOLEAN NOT NULL DEFAULT true,

  -- The JSON Schema document itself. JSONB for query and indexing.
  definition_jsonschema  JSONB NOT NULL,

  -- Owner. NULL for biodata_default (registry-wide); FK to organization
  -- for custom (R4.4).
  owner_org_id           UUID REFERENCES organization(id) ON DELETE RESTRICT,

  -- Audit. created_by FK into app_user. RESTRICT mirrors the rest of the
  -- audit columns in this schema.
  created_by             UUID NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),

  -- When the version was retired. NULL while in service. Set together
  -- with is_active = false by Validation_Lambda. Revalidation_Lambda
  -- consumes this field (R5.4).
  deprecated_at          TIMESTAMPTZ,

  -- Logical-name + version is unique registry-wide. design.md sketched
  -- (org_id, name, version); the task brief simplifies to (name, version)
  -- so the biodata_default chain (with owner_org_id IS NULL) participates
  -- in the same uniqueness universe and Validation_Lambda's cache key
  -- '{name}:{version}' is unambiguous without a tenant prefix. Custom
  -- schemas pick distinct names by convention (e.g. namespacing by org).
  CONSTRAINT schema_definition_name_version_unique UNIQUE (name, version),

  -- Invariants tying schema_kind to owner_org_id.
  CONSTRAINT schema_definition_owner_matches_kind CHECK (
    (schema_kind = 'biodata_default' AND owner_org_id IS NULL) OR
    (schema_kind = 'custom'          AND owner_org_id IS NOT NULL)
  ),

  -- An inactive schema must carry a deprecation timestamp; an active
  -- schema must not. Keeps the two flags in lock-step.
  CONSTRAINT schema_definition_active_deprecated_consistent CHECK (
    (is_active = true  AND deprecated_at IS NULL) OR
    (is_active = false AND deprecated_at IS NOT NULL)
  )
);

CREATE INDEX IF NOT EXISTS schema_definition_name_idx
  ON schema_definition (name);

CREATE INDEX IF NOT EXISTS schema_definition_owner_org_id_idx
  ON schema_definition (owner_org_id)
  WHERE owner_org_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS schema_definition_kind_idx
  ON schema_definition (schema_kind);

-- Validation_Lambda's hot-path query: "give me the active version of
-- schema X". A partial index on is_active = true keeps it small.
CREATE INDEX IF NOT EXISTS schema_definition_active_idx
  ON schema_definition (name, version)
  WHERE is_active = true;

COMMENT ON TABLE  schema_definition                        IS 'Registry-managed catalog of JSON schemas used by Validation_Lambda. R4, R5.';
COMMENT ON COLUMN schema_definition.id                     IS 'UUID primary key. Referenced by data_asset.schema_id (FK added at the bottom of this migration).';
COMMENT ON COLUMN schema_definition.name                   IS 'Logical schema name (programmatic identifier).';
COMMENT ON COLUMN schema_definition.version                IS 'Free-form version string (SemVer, calendar, or aind-data-schema release tag).';
COMMENT ON COLUMN schema_definition.schema_kind            IS '''biodata_default'' (owner_org_id NULL) or ''custom'' (owner_org_id required). R4.1, R4.4.';
COMMENT ON COLUMN schema_definition.is_active              IS 'Whether this version is currently in service. Flipped to false when retired (in lock-step with deprecated_at).';
COMMENT ON COLUMN schema_definition.definition_jsonschema  IS 'JSON Schema document (JSONB).';
COMMENT ON COLUMN schema_definition.owner_org_id           IS 'Owning Organization for custom schemas; NULL for biodata_default. R4.4.';
COMMENT ON COLUMN schema_definition.created_by             IS 'app_user that registered the schema version.';
COMMENT ON COLUMN schema_definition.created_at             IS 'Record creation timestamp (UTC).';
COMMENT ON COLUMN schema_definition.deprecated_at          IS 'When the version was retired (UTC). Consumed by Revalidation_Lambda (R5.4).';


-- =============================================================================
-- Cross-migration: data_asset.schema_id -> schema_definition(id)
-- =============================================================================
-- 0002_data_asset.sql created data_asset.schema_id as a bare UUID column
-- because schema_definition did not yet exist. Now that it does, add the
-- foreign-key constraint. ON DELETE RESTRICT prevents accidentally
-- deleting a schema that any asset is currently validating against; the
-- correct lifecycle is to mark the schema deprecated_at and let
-- Revalidation_Lambda re-route validation, not to delete the row.
--
-- Idempotency: Postgres has no ALTER TABLE ... ADD CONSTRAINT IF NOT
-- EXISTS, so we probe pg_constraint and skip the ALTER on re-runs.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM   pg_constraint
    WHERE  conname = 'data_asset_schema_id_fkey'
  ) THEN
    EXECUTE $ddl$
      ALTER TABLE data_asset
        ADD CONSTRAINT data_asset_schema_id_fkey
        FOREIGN KEY (schema_id)
        REFERENCES schema_definition(id)
        ON DELETE RESTRICT
    $ddl$;
  END IF;
END;
$$;

COMMENT ON CONSTRAINT data_asset_schema_id_fkey ON data_asset IS
  'FK from data_asset.schema_id to schema_definition.id. Added in 0005 because the target table is created here. ON DELETE RESTRICT — schemas are deprecated, never deleted.';

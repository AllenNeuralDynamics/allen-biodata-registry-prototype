-- =============================================================================
-- Migration: 0004_revisions_lifecycle_duplicates.sql
-- Purpose:   Audit + admin-workflow tables that ride alongside data_asset.
--            Creates the change_source_kind enum, the immutable
--            entity_revision audit log (REVOKE UPDATE, DELETE), the
--            lifecycle_transition history table, and the duplicate_flag
--            queue used by Duplicates_Lambda.
--
-- Spec:      .kiro/specs/allen-biodata-registry-poc
-- Task:      7.4 — Create migration 0004_revisions_lifecycle_duplicates.sql
-- Validates: R1.6, R6.1, R6.2, R6.3, R6.5, R26.1, R26.2, R26.5, R27.6
-- Design:    §Data Models.Aurora.Revisions, lifecycle history, duplicates
--            §Correctness Properties.Property 3 (Revision Immutability)
--            §Correctness Properties.Property 4 (Lifecycle State Machine)
--            §Correctness Properties.Property 9 (Duplicate Detection)
--
-- Idempotency:
--   * Tables and indexes use IF NOT EXISTS.
--   * The change_source_kind enum is wrapped in a DO block guarded by
--     `EXCEPTION WHEN duplicate_object` so re-runs do not error.
--   * REVOKE on entity_revision is intrinsically idempotent — revoking a
--     privilege that is not held is a no-op.
--   * Re-running this migration after a successful first run is a no-op.
--
-- Ordering:
--   * Depends on 0001_governance.sql (app_user) and 0002_data_asset.sql
--     (lifecycle_state enum, data_asset table). The migration runner
--     applies *.sql files in lexical order, so those prerequisites are
--     guaranteed to be in place.
--
-- Notes / deviations from the task brief:
--   * Authoritative DDL is design.md §Data Models.Aurora.Revisions,
--     lifecycle history, duplicates. The task brief proposes a richer
--     column shape (changed_by/changed_at on entity_revision,
--     transitioned_by/transitioned_at on lifecycle_transition,
--     candidate_data_asset_id + heuristic + status on duplicate_flag,
--     plus an 8-value change_source_kind). This migration follows
--     design.md because:
--       - Property 3 in §Correctness Properties pins the column names
--         (`user_id`, `timestamp`, `metadata_snapshot`, `previous_values`,
--         `new_values`, `change_source`, `revision_number`) and the
--         enum value set ({manual, agent, api, merge, ETL}). Renaming
--         these would silently invalidate the property test in Task 17.4.
--       - Property 4 pins `previous_state`/`new_state`/`user_id`/
--         `timestamp` for lifecycle_transition.
--       - R6.2 enumerates the same five-value change_source set.
--       - R26.5 calls out a dismiss decision rather than a multi-state
--         status — design.md's `dismissed` boolean + `dismissed_by` /
--         `dismissed_at` cleanly captures that.
--     The same pattern (design.md wins where the brief diverges) was
--     applied in 0001_governance.sql (role_kind values) and
--     0002_data_asset.sql (lifecycle_state / validation_status values).
--   * Additive niceties layered on top of design.md without breaking
--     downstream consumers:
--       - Indexes for the chronological-history query
--         (Revisions_Lambda's `GET /revisions?entity_type=X&entity_id=Y`
--         in design.md §Components.6) and the duplicate-review queue
--         (Duplicates_Lambda's "open flags" view).
--       - CHECK constraints for revision_number >= 1, similarity_score
--         in [0, 1], previous_state <> new_state, and a non-self-pair
--         constraint on duplicate_flag.
--       - Comment-only documentation that the application role's
--         INSERT/SELECT grants on entity_revision are configured in
--         0006_rls_policies.sql (alongside RLS) rather than here, to
--         keep this migration agnostic of which Postgres role the
--         Lambdas authenticate as.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Required extensions
-- -----------------------------------------------------------------------------

-- pgcrypto: gen_random_uuid() default for duplicate_flag.id. Already
-- created by 0001_governance.sql; the IF NOT EXISTS guard makes this a
-- no-op on re-run.
CREATE EXTENSION IF NOT EXISTS pgcrypto;


-- -----------------------------------------------------------------------------
-- Enum: change_source_kind
-- -----------------------------------------------------------------------------
-- Five-value vocabulary describing what kind of actor produced the
-- entity_revision row (R6.2). Property 3 in design.md §Correctness
-- Properties references exactly this set, so widening it requires a
-- coordinated schema + property-test update.
--
--   manual  — interactive user write through Registration_Lambda
--   agent   — MetaData_Agent / Curation_Agent write (R7.7)
--   api     — programmatic third-party API write (R6.4)
--   merge   — Duplicates_Lambda merge transaction (R26.4)
--   ETL     — Validation_Lambda re-validation pass (R5.4)
DO $$
BEGIN
  CREATE TYPE change_source_kind AS ENUM (
    'manual',
    'agent',
    'api',
    'merge',
    'ETL'
  );
EXCEPTION
  WHEN duplicate_object THEN
    -- already created on a prior run; nothing to do
    NULL;
END;
$$;

COMMENT ON TYPE change_source_kind IS
  'Actor kind for entity_revision.change_source. R6.2. Pinned by Property 3 in design.md §Correctness Properties.';


-- =============================================================================
-- Table: entity_revision
-- =============================================================================
-- Append-only audit log. Every create/update on a Data_Asset or shared
-- Metadata_Entity writes one row here (R1.6, R6.1). Reads are served by
-- Revisions_Lambda (`GET /revisions?entity_type=X&entity_id=Y`,
-- design.md §Components.6).
--
-- entity_id is polymorphic — it can reference data_asset.id, subject.id,
-- instrument.id, etc. — so we deliberately do NOT add a FK constraint;
-- the entity_type column carries the discriminator instead.
--
-- The UNIQUE (entity_type, entity_id, revision_number) constraint
-- enforces monotonic per-entity revision numbering. Registration_Lambda
-- computes the next revision_number with `MAX(revision_number) + 1`
-- inside the same transaction as the INSERT; the unique index serves as
-- the backstop if two transactions race.
CREATE TABLE IF NOT EXISTS entity_revision (
  id                BIGSERIAL    PRIMARY KEY,

  -- Polymorphic target. entity_type is the discriminator; entity_id is
  -- the row's PK in the corresponding table. No FK by design.
  entity_type       TEXT         NOT NULL,
  entity_id         UUID         NOT NULL,

  -- Monotonic per-entity revision counter (1, 2, 3, ...).
  revision_number   INT          NOT NULL,

  -- Who wrote the revision. NOT NULL because every audit row must be
  -- attributable to a known principal (R6.2). Lambda-driven writes
  -- (Validation_Lambda, Duplicates_Lambda) are attributed to the
  -- service-account app_user that the Lambda authenticates as.
  user_id           UUID         NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,

  -- Wall-clock time of the revision. Defaults to now() so callers do
  -- not have to set it explicitly.
  "timestamp"       TIMESTAMPTZ  NOT NULL DEFAULT now(),

  -- What kind of actor produced the row (see change_source_kind).
  change_source     change_source_kind NOT NULL,

  -- Full snapshot of the entity at this revision. NOT NULL — Property 3
  -- requires `metadata_snapshot` to always be populated so any historical
  -- state can be reconstructed without replaying diffs.
  metadata_snapshot JSONB        NOT NULL,

  -- Diffs. previous_values is NULL on create (revision_number = 1);
  -- new_values is NULL only on a hypothetical "tombstone" delete row
  -- which we currently never write (data_asset is never hard-deleted at
  -- PoC scale; archive is a lifecycle state, not a delete).
  previous_values   JSONB,
  new_values        JSONB,

  -- Per-entity revision_number must be positive.
  CONSTRAINT entity_revision_revision_number_positive
    CHECK (revision_number >= 1),

  -- Monotonic numbering invariant (per design.md and Property 3).
  CONSTRAINT entity_revision_unique_revision
    UNIQUE (entity_type, entity_id, revision_number)
);

-- Lookup index for the chronological history query
-- (`GET /revisions?entity_type=X&entity_id=Y`). Includes "timestamp"
-- DESC so the typical "show me the latest N revisions" query is an
-- index-only scan.
CREATE INDEX IF NOT EXISTS entity_revision_entity_lookup_idx
  ON entity_revision (entity_type, entity_id, "timestamp" DESC);

-- Lookup by user_id for "what did this user change?" admin queries
-- (R6.3) and for the right-to-be-forgotten audit footprint check.
CREATE INDEX IF NOT EXISTS entity_revision_user_id_idx
  ON entity_revision (user_id);

-- Lookup by change_source for system-vs-human attribution audits
-- (e.g. "show me every merge revision in the last 30 days").
CREATE INDEX IF NOT EXISTS entity_revision_change_source_idx
  ON entity_revision (change_source);

COMMENT ON TABLE  entity_revision                   IS 'Append-only audit log of every Data_Asset / Metadata_Entity write. R1.6, R6.1, R6.2, R23.3.';
COMMENT ON COLUMN entity_revision.id                IS 'BIGSERIAL primary key. Surrogate; do not rely on it for ordering across entities.';
COMMENT ON COLUMN entity_revision.entity_type       IS 'Discriminator naming the target table (''data_asset'', ''subject'', ''instrument'', ...). No FK — entity_id is polymorphic.';
COMMENT ON COLUMN entity_revision.entity_id         IS 'Polymorphic UUID pointing into the table named by entity_type.';
COMMENT ON COLUMN entity_revision.revision_number   IS 'Monotonic per-(entity_type, entity_id) revision counter, starting at 1. UNIQUE.';
COMMENT ON COLUMN entity_revision.user_id           IS 'app_user that wrote the revision. NOT NULL (R6.2).';
COMMENT ON COLUMN entity_revision."timestamp"       IS 'Wall-clock time of the revision (UTC). Defaults to now().';
COMMENT ON COLUMN entity_revision.change_source     IS 'Actor kind. See change_source_kind (R6.2). Pinned by Property 3.';
COMMENT ON COLUMN entity_revision.metadata_snapshot IS 'Full JSONB snapshot of the entity at this revision. NOT NULL (Property 3).';
COMMENT ON COLUMN entity_revision.previous_values   IS 'JSONB diff of fields prior to this revision. NULL on the create row (revision_number = 1).';
COMMENT ON COLUMN entity_revision.new_values        IS 'JSONB diff of fields written by this revision. NULL only on tombstone rows (not currently written).';


-- -----------------------------------------------------------------------------
-- Immutability: REVOKE UPDATE, DELETE on entity_revision FROM PUBLIC
-- -----------------------------------------------------------------------------
-- R6.1: revisions are append-only. Revoking from PUBLIC removes the
-- default "any role can do anything" bootstrap permission so that no
-- non-superuser can mutate or remove rows. The application role
-- (used by Registration_Lambda) will be granted INSERT, SELECT
-- explicitly in 0006_rls_policies.sql; that grant intentionally does
-- NOT include UPDATE or DELETE.
--
-- The migration runner authenticates as the cluster bootstrap role,
-- which retains UPDATE/DELETE via ownership rather than the PUBLIC
-- grant; this REVOKE therefore does not lock the runner out.
--
-- REVOKE is idempotent: revoking a privilege that was never held (or
-- has already been revoked) is a no-op.
REVOKE UPDATE, DELETE ON entity_revision FROM PUBLIC;


-- =============================================================================
-- Table: lifecycle_transition
-- =============================================================================
-- One row per successful Data_Asset lifecycle state change (R27.6).
-- Lifecycle_Lambda writes this row in the same transaction that bumps
-- data_asset.lifecycle_state (design.md §Components.4). Property 4
-- asserts that exactly one row appears per accepted transition with the
-- correct previous_state, new_state, user_id, and timestamp.
--
-- previous_state and new_state both reference the lifecycle_state enum
-- created in 0002_data_asset.sql. previous_state is NOT NULL because
-- there is no "create from nothing" transition — assets land in 'draft'
-- with no transition row, and every subsequent state move has a real
-- predecessor.
CREATE TABLE IF NOT EXISTS lifecycle_transition (
  id              BIGSERIAL       PRIMARY KEY,

  -- The asset whose state changed. RESTRICT on delete because
  -- transition history must outlive the asset for audit purposes; the
  -- asset deletion path archives instead of hard-deleting.
  data_asset_id   UUID            NOT NULL REFERENCES data_asset(id) ON DELETE RESTRICT,

  -- Who performed the transition. NOT NULL — every transition is
  -- attributable to a real principal (R27.6).
  user_id         UUID            NOT NULL REFERENCES app_user(id)  ON DELETE RESTRICT,

  -- Wall-clock time of the transition.
  "timestamp"     TIMESTAMPTZ     NOT NULL DEFAULT now(),

  -- State machine endpoints.
  previous_state  lifecycle_state NOT NULL,
  new_state       lifecycle_state NOT NULL,

  -- A self-transition (draft → draft) is meaningless and a sign of a
  -- bug in Lifecycle_Lambda's accept-or-reject logic.
  CONSTRAINT lifecycle_transition_state_changes
    CHECK (previous_state <> new_state)
);

-- Per-asset chronological history. The DESC ordering makes
-- "show me the most recent transitions for asset X" an index-only scan
-- and is the access pattern Lifecycle_Lambda uses to compute
-- `allowed_transitions` for the INVALID_STATE_TRANSITION error path
-- (design.md §Components.4, R27.3).
CREATE INDEX IF NOT EXISTS lifecycle_transition_data_asset_id_idx
  ON lifecycle_transition (data_asset_id, "timestamp" DESC);

-- Lookup by user_id for admin "who pushed what to published" audits.
CREATE INDEX IF NOT EXISTS lifecycle_transition_user_id_idx
  ON lifecycle_transition (user_id);

COMMENT ON TABLE  lifecycle_transition                IS 'Audit history of Data_Asset lifecycle state changes. R27.6. One row per successful transition.';
COMMENT ON COLUMN lifecycle_transition.id             IS 'BIGSERIAL primary key.';
COMMENT ON COLUMN lifecycle_transition.data_asset_id  IS 'Asset whose state changed. ON DELETE RESTRICT — history outlives the asset.';
COMMENT ON COLUMN lifecycle_transition.user_id        IS 'app_user that performed the transition. NOT NULL.';
COMMENT ON COLUMN lifecycle_transition."timestamp"    IS 'Wall-clock time of the transition (UTC). Defaults to now().';
COMMENT ON COLUMN lifecycle_transition.previous_state IS 'lifecycle_state before the transition.';
COMMENT ON COLUMN lifecycle_transition.new_state      IS 'lifecycle_state after the transition. Must differ from previous_state.';


-- =============================================================================
-- Table: duplicate_flag
-- =============================================================================
-- Queue of suspected-duplicate entity pairs awaiting admin review
-- (R3.4, R26.2). Duplicates_Lambda inserts a row when a similarity
-- score crosses the warn threshold (synchronous on register or
-- background scan, design.md §Components.5). An admin can dismiss a
-- pair (R26.5) or merge them (R26.3); merging is performed by the
-- separate merge transaction, which then writes an entity_revision
-- with change_source = 'merge'.
--
-- entity_a_id and entity_b_id are polymorphic — typed by entity_type —
-- so we do not add a FK constraint. The CHECK below prevents the
-- degenerate self-pair case (an entity flagged as a duplicate of
-- itself) which would always be a Duplicates_Lambda bug.
--
-- The UNIQUE (entity_type, entity_a_id, entity_b_id) constraint stops
-- the same pair from being re-flagged on every background scan; the
-- detector deduplicates by ON CONFLICT DO NOTHING. R26.5 — dismissing a
-- pair sets dismissed = true; the row stays in place so subsequent
-- scans hit the unique constraint and skip.
CREATE TABLE IF NOT EXISTS duplicate_flag (
  id                UUID         PRIMARY KEY DEFAULT gen_random_uuid(),

  -- Discriminator naming the target table for entity_a_id / entity_b_id.
  entity_type       TEXT         NOT NULL,

  -- The two entities suspected of being duplicates.
  entity_a_id       UUID         NOT NULL,
  entity_b_id       UUID         NOT NULL,

  -- pgvector cosine / SQL similarity score in [0, 1]. NUMERIC(5,4)
  -- gives four decimal places of precision, which is the resolution
  -- Duplicates_Lambda exposes to the API.
  similarity_score  NUMERIC(5,4) NOT NULL,

  -- When the pair was first flagged. NOT a default-NULL: a flag without
  -- a flagged_at timestamp would corrupt the duplicate-review queue.
  flagged_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),

  -- Dismissal state (R26.5). dismissed = true means an admin has
  -- decided the pair are not duplicates; the unique constraint then
  -- prevents the same pair being re-flagged.
  dismissed         BOOLEAN      NOT NULL DEFAULT false,
  dismissed_by      UUID         REFERENCES app_user(id) ON DELETE SET NULL,
  dismissed_at      TIMESTAMPTZ,

  -- A pair must be two distinct entities — flagging an entity as a
  -- duplicate of itself is always a Duplicates_Lambda bug.
  CONSTRAINT duplicate_flag_distinct_pair
    CHECK (entity_a_id <> entity_b_id),

  -- Score sanity: pgvector cosine and similarity() both produce
  -- values in [0, 1].
  CONSTRAINT duplicate_flag_score_range
    CHECK (similarity_score >= 0 AND similarity_score <= 1),

  -- Dismissal is atomic: dismissed_by and dismissed_at are set
  -- together, and only when dismissed = true. dismissed = true also
  -- requires the metadata to be populated — a "ghost dismissal" with
  -- null reviewer/timestamp would defeat audit (R26.5).
  CONSTRAINT duplicate_flag_dismissal_consistent
    CHECK (
      (NOT dismissed AND dismissed_by IS NULL AND dismissed_at IS NULL) OR
      (dismissed     AND dismissed_by IS NOT NULL AND dismissed_at IS NOT NULL)
    ),

  -- A pair is unique by (entity_type, a, b). Duplicates_Lambda canon-
  -- icalises the pair (smaller UUID first) before insert so the order
  -- is deterministic, but we cannot enforce that ordering at the
  -- schema level without a generated column; the canonicalisation is
  -- the caller's contract (design.md §Components.5).
  CONSTRAINT duplicate_flag_unique_pair
    UNIQUE (entity_type, entity_a_id, entity_b_id)
);

-- Lookup by either side of the pair. A duplicate-review UI typically
-- asks "what flags involve asset X?" — we want to answer that without
-- a sequential scan, so we index both sides separately.
CREATE INDEX IF NOT EXISTS duplicate_flag_entity_a_idx
  ON duplicate_flag (entity_type, entity_a_id);

CREATE INDEX IF NOT EXISTS duplicate_flag_entity_b_idx
  ON duplicate_flag (entity_type, entity_b_id);

-- Open-flags queue: the Duplicates_Lambda admin endpoint shows
-- undismissed flags ordered by flagged_at DESC. A partial index keeps
-- this small even after thousands of historical dismissals.
CREATE INDEX IF NOT EXISTS duplicate_flag_open_flagged_at_idx
  ON duplicate_flag (flagged_at DESC)
  WHERE dismissed = false;

COMMENT ON TABLE  duplicate_flag                  IS 'Suspected-duplicate entity pairs awaiting admin review. R3.4, R26.1, R26.2, R26.5.';
COMMENT ON COLUMN duplicate_flag.id               IS 'UUID primary key.';
COMMENT ON COLUMN duplicate_flag.entity_type      IS 'Discriminator naming the table for entity_a_id / entity_b_id.';
COMMENT ON COLUMN duplicate_flag.entity_a_id      IS 'First entity of the suspected pair (canonical ordering: smaller UUID first).';
COMMENT ON COLUMN duplicate_flag.entity_b_id      IS 'Second entity of the suspected pair (must differ from entity_a_id).';
COMMENT ON COLUMN duplicate_flag.similarity_score IS 'Similarity score in [0, 1]. pgvector cosine or SQL similarity().';
COMMENT ON COLUMN duplicate_flag.flagged_at       IS 'When the pair was first flagged (UTC). Defaults to now().';
COMMENT ON COLUMN duplicate_flag.dismissed        IS 'Admin dismissal flag. true = "not actually a duplicate" (R26.5).';
COMMENT ON COLUMN duplicate_flag.dismissed_by     IS 'app_user that dismissed the flag. NOT NULL when dismissed = true.';
COMMENT ON COLUMN duplicate_flag.dismissed_at     IS 'When the flag was dismissed (UTC). NOT NULL when dismissed = true.';

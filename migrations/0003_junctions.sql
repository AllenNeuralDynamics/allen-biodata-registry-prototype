-- =============================================================================
-- Migration: 0003_junctions.sql
-- Purpose:   Many-to-many junction tables linking data_asset to the four
--            *shared* biological entities (subject, instrument, rig,
--            procedures). Together with the FK columns that already exist
--            on the asset-specific entities (session, acquisition,
--            processing, quality_control, data_description) created in
--            0002_data_asset.sql, this completes the data_asset relational
--            graph.
--
-- Spec:      .kiro/specs/allen-biodata-registry-poc
-- Task:      7.3 — Create migration 0003_junctions.sql
-- Validates: R2.5, R25.1
-- Design:    §Data Models.Aurora.Data_Asset and shared entity tables
--            §Correctness Properties.Property 10 (Shared vs Asset-Specific
--            Entity Lifecycle)
--
-- Idempotency:
--   * All junction tables and indexes use IF NOT EXISTS.
--   * Re-running this migration after a successful first run is a no-op.
--   * Depends on tables created by 0002_data_asset.sql (data_asset, subject,
--     instrument, rig, procedures) — the migration runner enforces lexical
--     ordering.
--
-- =============================================================================
-- Shared vs asset-specific entities — what gets a junction and why
-- =============================================================================
-- design.md §Overview.Guiding Principles:
--   "Shared vs. asset-specific entities. Subject, Instrument, Procedures, Rig
--    are reusable and shared by reference. Session, Acquisition,
--    DataDescription, Processing, QualityControl are tied to a specific
--    Data_Asset."
--
-- design.md §Correctness Properties.Property 10 makes the lifecycle contract
-- explicit:
--   "every asset-specific entity (Session, Acquisition, Processing,
--    QualityControl, DataDescription) with data_asset_id = A.id SHALL be
--    cascade-deleted, while every shared entity referenced by A SHALL
--    remain intact."
--
-- That contract dictates which entities need a junction:
--
--   ┌──────────────────┬─────────────┬─────────────────────────────────────┐
--   │ Entity           │ Cardinality │ How linked to data_asset            │
--   ├──────────────────┼─────────────┼─────────────────────────────────────┤
--   │ subject          │ M : N       │ data_asset_subject  (this file)     │
--   │ instrument       │ M : N       │ data_asset_instrument (this file)   │
--   │ rig              │ M : N       │ data_asset_rig (this file)          │
--   │ procedures       │ M : N       │ data_asset_procedures (this file)   │
--   ├──────────────────┼─────────────┼─────────────────────────────────────┤
--   │ session          │ 1 : N       │ session.data_asset_id  (in 0002)    │
--   │ acquisition      │ 1 : N       │ acquisition.data_asset_id  (0002)   │
--   │ processing       │ 1 : N       │ processing.data_asset_id  (0002)   │
--   │ quality_control  │ 1 : N       │ quality_control.data_asset_id (0002)│
--   │ data_description │ 1 : N       │ data_description.data_asset_id 0002 │
--   └──────────────────┴─────────────┴─────────────────────────────────────┘
--
-- The task brief lists data_asset_session, data_asset_acquisition,
-- data_asset_processing, data_asset_quality_control, and
-- data_asset_data_description as candidates and asks us to "verify against
-- design.md and pick one pattern". Property 10 is unambiguous: those five
-- entities are strict 1:N from data_asset, with the FK living on the child
-- entity itself (already created in 0002). Adding a junction would model a
-- many-to-many relationship that the design explicitly forbids — a
-- Processing run, a QualityControl record, etc. each belong to exactly
-- one Data_Asset and are deleted with it. We therefore DO NOT create
-- those five junctions.
--
-- =============================================================================
-- Foreign-key delete semantics
-- =============================================================================
-- design.md models junctions with the asymmetric pattern:
--
--     ON DELETE CASCADE   on the data_asset side
--     ON DELETE RESTRICT  on the shared-entity side
--
-- Rationale:
--   * Deleting a Data_Asset legitimately cascades to its junction rows so
--     the asset is removed cleanly (Property 10: shared entities themselves
--     remain intact, only their *links* to the deleted asset go away).
--   * Deleting a shared entity (Subject, Instrument, Rig, Procedures) while
--     it is still referenced by any Data_Asset would silently break the
--     asset's metadata graph. RESTRICT forces the operator to consciously
--     un-link or migrate first, surfacing dangling-reference bugs early.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- Table: data_asset_subject
-- -----------------------------------------------------------------------------
-- Many-to-many link between Data_Asset and shared Subject entities. A single
-- Data_Asset may reference multiple Subjects (e.g. a multi-animal session)
-- and a single Subject may participate in many Data_Assets across studies
-- (R25.1, the canonical motivation for de-duplication).
CREATE TABLE IF NOT EXISTS data_asset_subject (
  data_asset_id UUID NOT NULL REFERENCES data_asset(id) ON DELETE CASCADE,
  subject_id    UUID NOT NULL REFERENCES subject(id)    ON DELETE RESTRICT,
  linked_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (data_asset_id, subject_id)
);

-- Reverse-lookup index: "find all data_assets that reference subject X".
-- The composite PK already covers the (data_asset_id, ...) direction.
CREATE INDEX IF NOT EXISTS data_asset_subject_subject_id_idx
  ON data_asset_subject (subject_id);

COMMENT ON TABLE  data_asset_subject               IS 'M:N junction between data_asset and shared subject. R2.5, R25.1.';
COMMENT ON COLUMN data_asset_subject.data_asset_id IS 'FK -> data_asset(id). ON DELETE CASCADE: deleting an asset removes its links.';
COMMENT ON COLUMN data_asset_subject.subject_id    IS 'FK -> subject(id). ON DELETE RESTRICT: shared subjects must be unlinked before deletion.';
COMMENT ON COLUMN data_asset_subject.linked_at     IS 'When the link was established (UTC). Audit aid; revision history lives in entity_revision.';


-- -----------------------------------------------------------------------------
-- Table: data_asset_instrument
-- -----------------------------------------------------------------------------
-- Many-to-many link between Data_Asset and shared Instrument entities. A
-- single Data_Asset may reference multiple Instruments (e.g. a session that
-- combines a microscope + an electrophysiology rig) and a single Instrument
-- is reused across many assets.
CREATE TABLE IF NOT EXISTS data_asset_instrument (
  data_asset_id UUID NOT NULL REFERENCES data_asset(id) ON DELETE CASCADE,
  instrument_id UUID NOT NULL REFERENCES instrument(id) ON DELETE RESTRICT,
  linked_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (data_asset_id, instrument_id)
);

CREATE INDEX IF NOT EXISTS data_asset_instrument_instrument_id_idx
  ON data_asset_instrument (instrument_id);

COMMENT ON TABLE  data_asset_instrument               IS 'M:N junction between data_asset and shared instrument. R2.5, R25.1.';
COMMENT ON COLUMN data_asset_instrument.data_asset_id IS 'FK -> data_asset(id). ON DELETE CASCADE: deleting an asset removes its links.';
COMMENT ON COLUMN data_asset_instrument.instrument_id IS 'FK -> instrument(id). ON DELETE RESTRICT: shared instruments must be unlinked before deletion.';
COMMENT ON COLUMN data_asset_instrument.linked_at     IS 'When the link was established (UTC). Audit aid; revision history lives in entity_revision.';


-- -----------------------------------------------------------------------------
-- Table: data_asset_rig
-- -----------------------------------------------------------------------------
-- Many-to-many link between Data_Asset and shared Rig entities. Rig is a
-- shared physical apparatus reusable across assets per
-- design.md §Overview.Guiding Principles. design.md does not include this
-- junction in its sketch but the task brief calls it out and rig is
-- semantically symmetric with instrument (also shared, also referenced by
-- session.rig_id) — adding the junction makes the M:N model explicit at
-- the data_asset level the same way data_asset_instrument does.
CREATE TABLE IF NOT EXISTS data_asset_rig (
  data_asset_id UUID NOT NULL REFERENCES data_asset(id) ON DELETE CASCADE,
  rig_id        UUID NOT NULL REFERENCES rig(id)        ON DELETE RESTRICT,
  linked_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (data_asset_id, rig_id)
);

CREATE INDEX IF NOT EXISTS data_asset_rig_rig_id_idx
  ON data_asset_rig (rig_id);

COMMENT ON TABLE  data_asset_rig               IS 'M:N junction between data_asset and shared rig. R2.5, R25.1.';
COMMENT ON COLUMN data_asset_rig.data_asset_id IS 'FK -> data_asset(id). ON DELETE CASCADE: deleting an asset removes its links.';
COMMENT ON COLUMN data_asset_rig.rig_id        IS 'FK -> rig(id). ON DELETE RESTRICT: shared rigs must be unlinked before deletion.';
COMMENT ON COLUMN data_asset_rig.linked_at     IS 'When the link was established (UTC). Audit aid; revision history lives in entity_revision.';


-- -----------------------------------------------------------------------------
-- Table: data_asset_procedures
-- -----------------------------------------------------------------------------
-- Many-to-many link between Data_Asset and shared Procedures entities.
-- Procedures itself has a 1:N back-reference to subject (procedures.subject_id
-- in 0002, per R25.2) — that link models which Subject a Procedure was
-- performed on. The link to Data_Asset is independent: a single Procedure
-- record (e.g. a surgery) can be referenced by multiple downstream assets,
-- and a single asset may aggregate multiple procedures.
CREATE TABLE IF NOT EXISTS data_asset_procedures (
  data_asset_id UUID NOT NULL REFERENCES data_asset(id) ON DELETE CASCADE,
  procedures_id UUID NOT NULL REFERENCES procedures(id) ON DELETE RESTRICT,
  linked_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (data_asset_id, procedures_id)
);

CREATE INDEX IF NOT EXISTS data_asset_procedures_procedures_id_idx
  ON data_asset_procedures (procedures_id);

COMMENT ON TABLE  data_asset_procedures               IS 'M:N junction between data_asset and shared procedures. R2.5, R25.1.';
COMMENT ON COLUMN data_asset_procedures.data_asset_id IS 'FK -> data_asset(id). ON DELETE CASCADE: deleting an asset removes its links.';
COMMENT ON COLUMN data_asset_procedures.procedures_id IS 'FK -> procedures(id). ON DELETE RESTRICT: shared procedures must be unlinked before deletion.';
COMMENT ON COLUMN data_asset_procedures.linked_at     IS 'When the link was established (UTC). Audit aid; revision history lives in entity_revision.';

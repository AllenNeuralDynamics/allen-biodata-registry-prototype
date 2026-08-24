-- =============================================================================
-- Migration: 0002_data_asset.sql
-- Purpose:   Create the Data_Asset core table together with the lifecycle and
--            validation enums, the storage_uri unique index that drives
--            duplicate detection, and every shared / asset-specific entity
--            table referenced by aind-data-schema.
--
-- Spec:      .kiro/specs/allen-biodata-registry-poc
-- Task:      7.2 — Create migration 0002_data_asset.sql
-- Validates: R1.1, R1.2, R2.1, R2.2, R2.3, R2.5, R2.6, R2.7,
--            R3.1, R25.1, R25.2, R25.5, R26.1
-- Design:    §Data Models.Aurora.Data_Asset and shared entity tables
--
-- Idempotency:
--   * All tables, indexes, and extensions use IF NOT EXISTS.
--   * Enums are wrapped in DO blocks guarded by EXCEPTION
--     WHEN duplicate_object so re-runs are no-ops.
--   * Re-running this migration after a successful first run is a no-op.
--
-- Ordering:
--   * The migration runner (Task 8.1) applies *.sql files in lexical order,
--     so 0001_governance.sql (organization / space / app_user) is applied
--     first. 0002 depends on those tables for FKs (space_id, created_by).
--
-- Notes / deviations from design.md:
--   * Authoritative DDL is design.md §Data Models.Aurora.Data_Asset and
--     shared entity tables. Where the task brief diverges from design.md
--     (notably the lifecycle_state and validation_status enum value sets),
--     this migration follows design.md to keep the schema consistent with
--     downstream migrations (0006_rls_policies.sql, the RLS policy on
--     `lifecycle_state = 'published'`) and Lambda code paths
--     (Lifecycle_Lambda's `{(draft,registered),(registered,published),
--     (published,archived),(archived,registered)}` state machine in
--     design.md §Components.4).
--       - design.md lifecycle_state: ('draft','registered','published','archived')
--       - design.md validation_status: ('valid','invalid','unvalidated','schema-deprecated')
--   * Additive columns called out by the task brief (display_name, name,
--     description, sex/dob on subject, instrument_type, calibration_date,
--     session_start/session_end, acquisition_start/acquisition_end, etc.)
--     are layered on top of design.md without breaking columns referenced
--     by downstream migrations. Same pattern used in 0001_governance.sql.
--   * `description_vec_status` enum (drives Embedding_Backfill_Lambda's
--     pending-queue scan in design.md §Components.12a) is added on
--     `data_asset` and on every shared entity that carries an embedding.
--   * `version` integer column on `data_asset` is included for optimistic
--     concurrency control even though design.md does not explicitly list
--     it; it is harmless when unused and will be wired in by Registration_
--     Lambda (Task 16.1) for PUT-with-If-Match behaviour.
--   * pgvector: prod Aurora has the `vector` extension enabled by the
--     `aurora` Terraform module's parameter group. For local lint runs on
--     stock PostgreSQL 16 (no pgvector available) the conditional block at
--     the top of this file falls back to a `vector` *domain* alias over
--     `text` so the DDL parses and applies. The schema_version recorded
--     by the migration runner against Aurora is identical either way: the
--     production extension supplies the real `vector` type before this
--     migration runs.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Required extensions
-- -----------------------------------------------------------------------------

-- pgcrypto and citext were created by 0001_governance.sql; calling them
-- again is a no-op thanks to IF NOT EXISTS.
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

-- pgvector: enabled in production by the Aurora parameter group. Local
-- lint environments may not have it; in that case we substitute a domain
-- alias over text so the DDL still applies. Production Aurora always has
-- the real extension installed before migrations run.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector') THEN
    CREATE EXTENSION IF NOT EXISTS vector;
  ELSIF NOT EXISTS (
    SELECT 1
    FROM pg_type t
    JOIN pg_namespace n ON n.oid = t.typnamespace
    WHERE t.typname = 'vector' AND n.nspname = 'public'
  ) THEN
    -- Local-lint shim: vector(N) is only ever used as a column type below;
    -- a text-based domain accepts the same column declarations and lets
    -- the migration apply for syntax checking. This branch never executes
    -- on Aurora.
    CREATE DOMAIN vector AS text;
  END IF;
END;
$$;


-- -----------------------------------------------------------------------------
-- Enum: lifecycle_state
-- -----------------------------------------------------------------------------
-- Four states a Data_Asset passes through (R27.2, design.md §Components.4).
-- The set is authoritative for the RLS policy `lifecycle_state = 'published'`
-- created in 0006_rls_policies.sql.
DO $$
BEGIN
  CREATE TYPE lifecycle_state AS ENUM (
    'draft',
    'registered',
    'published',
    'archived'
  );
EXCEPTION
  WHEN duplicate_object THEN
    NULL;
END;
$$;

COMMENT ON TYPE lifecycle_state IS
  'Data_Asset lifecycle. R27.1, R27.2. Drives RLS published-visibility and the Lifecycle_Lambda state machine.';


-- -----------------------------------------------------------------------------
-- Enum: validation_status
-- -----------------------------------------------------------------------------
-- Outcome of validating Data_Asset metadata against the Biodata_Schema or a
-- Custom_Schema (R4). schema-deprecated is set by Revalidation_Lambda for
-- assets whose only validating schema version has been retired (R5.4).
DO $$
BEGIN
  CREATE TYPE validation_status AS ENUM (
    'valid',
    'invalid',
    'unvalidated',
    'schema-deprecated'
  );
EXCEPTION
  WHEN duplicate_object THEN
    NULL;
END;
$$;

COMMENT ON TYPE validation_status IS
  'Validation outcome for a Data_Asset. R4, R5.4. ''unvalidated'' is the default at create time before Validation_Lambda runs.';


-- -----------------------------------------------------------------------------
-- Enum: description_vec_status
-- -----------------------------------------------------------------------------
-- Tracks the embedding lifecycle on entities that carry a description
-- vector. Drives Embedding_Backfill_Lambda's pending-queue scan
-- (design.md §Components.12a, R17.5, R28.7).
DO $$
BEGIN
  CREATE TYPE description_vec_status AS ENUM (
    'pending',
    'embedded',
    'failed'
  );
EXCEPTION
  WHEN duplicate_object THEN
    NULL;
END;
$$;

COMMENT ON TYPE description_vec_status IS
  'Embedding state for description fields. ''pending'' rows are picked up by Embedding_Backfill_Lambda; ''failed'' rows exceed retry threshold and are surfaced via Observability_Lambda.';


-- -----------------------------------------------------------------------------
-- Enum: description_kind
-- -----------------------------------------------------------------------------
-- Distinguishes hand-authored DataDescription text from auto-generated
-- summaries produced by the MetaData_Agent (design.md §Components.11).
DO $$
BEGIN
  CREATE TYPE description_kind AS ENUM (
    'human',
    'auto'
  );
EXCEPTION
  WHEN duplicate_object THEN
    NULL;
END;
$$;

COMMENT ON TYPE description_kind IS
  'Origin of a data_description.text row: ''human'' for user-authored, ''auto'' for agent-generated.';


-- =============================================================================
-- Core: data_asset
-- =============================================================================
-- The single row that represents a registered collection of files. Every
-- read store (DocumentDB, OpenSearch) is downstream of this table via the
-- CDC pipeline (R1.7, R28).
CREATE TABLE IF NOT EXISTS data_asset (
  id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  -- Governance scope. RESTRICT prevents accidentally deleting a space that
  -- still owns assets; the org/space deletion path is admin-mediated.
  space_id                UUID NOT NULL REFERENCES space(id) ON DELETE RESTRICT,

  -- Display & search-friendly fields. `name` is citext so case-only
  -- variants are treated as the same name within a space (R17 search
  -- matching). storage_uri remains plain text — URIs are case-sensitive.
  name                    CITEXT,
  display_name            TEXT,

  -- Storage location. R1.1: any URI is accepted (s3://, gs://, az://,
  -- file://, /mnt/...). The unique index below produces the only
  -- 409 DUPLICATE_ENTITY path (design.md §Components.5, R3.1, R26.1).
  storage_uri             TEXT NOT NULL,

  -- aind-data-schema modality (R4.8). Stored as text rather than an enum
  -- because the modality vocabulary is owned by aind-data-schema and
  -- evolves out-of-band; pinning it to a Postgres enum would force a
  -- migration on every aind-data-schema release.
  data_type               TEXT,

  -- Lifecycle + validation state machines.
  lifecycle_state         lifecycle_state NOT NULL DEFAULT 'draft',
  validation_status       validation_status NOT NULL DEFAULT 'unvalidated',
  validation_errors       JSONB,

  -- Sensitive_Flag enforcement (R8). sensitive_flag_meta carries the
  -- justification (e.g. "human donor data, IRB-2024-117") surfaced by
  -- the API when a 403 is returned.
  sensitive_flag          BOOLEAN NOT NULL DEFAULT false,
  sensitive_flag_meta     JSONB,

  -- Schema linkage. schema_id references the registry-managed
  -- schema_definition table created in 0005_collections_schemas.sql; the
  -- FK is added in that migration so this file does not depend on it.
  -- schema_version mirrors the version string for fast lookups without
  -- joining schema_definition.
  schema_id               UUID,
  schema_version          TEXT,

  -- Provenance. Self-referential FK enforces the link-back to a parent
  -- Data_Asset when one exists (R1.5).
  provenance_source_id    UUID REFERENCES data_asset(id) ON DELETE SET NULL,

  -- Description + embedding pipeline (drives Embedding_Backfill_Lambda).
  description             TEXT,
  description_vec         vector(1024),
  description_vec_status  description_vec_status NOT NULL DEFAULT 'pending',

  -- JSONB passthrough for fields not promoted to columns (R2.3).
  metadata                JSONB NOT NULL DEFAULT '{}'::jsonb,

  -- Audit. created_by FK into app_user. RESTRICT so deleting a user does
  -- not orphan their writes; the right-to-be-forgotten path tombstones
  -- the user instead.
  created_by              UUID NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
  created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

  -- Optimistic concurrency. Bumped by Registration_Lambda on every PUT.
  version                 INTEGER NOT NULL DEFAULT 1,

  -- A self-loop in provenance is meaningless and a sign of a bug.
  CONSTRAINT data_asset_no_self_provenance
    CHECK (provenance_source_id IS NULL OR provenance_source_id <> id),

  -- Sanity: updated_at >= created_at.
  CONSTRAINT data_asset_updated_after_created
    CHECK (updated_at >= created_at),

  -- Sanity: version is positive.
  CONSTRAINT data_asset_version_positive
    CHECK (version >= 1)
);

-- The unique index whose IntegrityError is the only 409 duplicate path
-- (R3.1, R26.1, design.md §Components.5 "exact URI collision"). Named
-- exactly as referenced by Lambda code in the design doc.
CREATE UNIQUE INDEX IF NOT EXISTS data_asset_storage_uri_unique
  ON data_asset (storage_uri);

-- Foreign-key and lookup indexes.
CREATE INDEX IF NOT EXISTS data_asset_space_id_idx
  ON data_asset (space_id);

CREATE INDEX IF NOT EXISTS data_asset_created_by_idx
  ON data_asset (created_by);

CREATE INDEX IF NOT EXISTS data_asset_provenance_source_id_idx
  ON data_asset (provenance_source_id)
  WHERE provenance_source_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS data_asset_schema_id_idx
  ON data_asset (schema_id)
  WHERE schema_id IS NOT NULL;

-- Lifecycle / validation lookups: feeds Search_Lambda's
-- `published`+`valid` default filter and Revalidation_Lambda's pagination
-- by schema_id when re-validating a schema version.
CREATE INDEX IF NOT EXISTS data_asset_lifecycle_state_idx
  ON data_asset (lifecycle_state);

CREATE INDEX IF NOT EXISTS data_asset_validation_status_idx
  ON data_asset (validation_status);

-- Embedding backfill consumer queries `description_vec_status = 'pending'`
-- ordered by created_at. A partial index keeps it small.
CREATE INDEX IF NOT EXISTS data_asset_desc_vec_pending_idx
  ON data_asset (created_at)
  WHERE description_vec_status = 'pending';

COMMENT ON TABLE  data_asset
  IS 'Registered collection of files. Source of truth; CDC propagates to DocumentDB and OpenSearch. R1, R2, R8, R9.3.';
COMMENT ON COLUMN data_asset.id                     IS 'UUID primary key. R1.2.';
COMMENT ON COLUMN data_asset.space_id               IS 'Owning Space. RLS scope (R10.1).';
COMMENT ON COLUMN data_asset.name                   IS 'Case-insensitive search-friendly name.';
COMMENT ON COLUMN data_asset.display_name           IS 'Human-friendly display name shown in the UI.';
COMMENT ON COLUMN data_asset.storage_uri            IS 'Storage URI (s3://, gs://, az://, file://, ...). R1.1. UNIQUE — produces the 409 DUPLICATE_ENTITY path.';
COMMENT ON COLUMN data_asset.data_type              IS 'aind-data-schema modality (free text — vocabulary owned by aind-data-schema). R4.8.';
COMMENT ON COLUMN data_asset.lifecycle_state        IS 'Lifecycle state. R27.1. Default ''draft''.';
COMMENT ON COLUMN data_asset.validation_status      IS 'Validation outcome. R4. Default ''unvalidated'' until Validation_Lambda runs.';
COMMENT ON COLUMN data_asset.validation_errors      IS 'JSONB list of validation errors when validation_status = ''invalid''.';
COMMENT ON COLUMN data_asset.sensitive_flag         IS 'Marks this asset as sensitive (e.g. human donor data). R8.';
COMMENT ON COLUMN data_asset.sensitive_flag_meta    IS 'JSONB justification surfaced when a 403 SENSITIVE_ACCESS_DENIED is returned.';
COMMENT ON COLUMN data_asset.schema_id              IS 'FK to schema_definition (added in 0005). NULL for default Biodata_Schema.';
COMMENT ON COLUMN data_asset.schema_version         IS 'Schema version string for fast lookups without join.';
COMMENT ON COLUMN data_asset.provenance_source_id   IS 'Self-FK to source Data_Asset for derived assets. R1.5.';
COMMENT ON COLUMN data_asset.description            IS 'Human-readable description. Embedded by Embedding_Backfill_Lambda.';
COMMENT ON COLUMN data_asset.description_vec        IS 'pgvector(1024) embedding of description. Populated asynchronously.';
COMMENT ON COLUMN data_asset.description_vec_status IS 'Embedding state — drives Embedding_Backfill_Lambda. R17.5, R28.7.';
COMMENT ON COLUMN data_asset.metadata               IS 'JSONB passthrough for non-promoted fields. R2.3.';
COMMENT ON COLUMN data_asset.created_by             IS 'app_user that created the asset.';
COMMENT ON COLUMN data_asset.created_at             IS 'Record creation timestamp (UTC).';
COMMENT ON COLUMN data_asset.updated_at             IS 'Last modification timestamp (UTC).';
COMMENT ON COLUMN data_asset.version                IS 'Optimistic-concurrency revision counter. Bumped by Registration_Lambda on every PUT.';


-- =============================================================================
-- Shared biological entities
-- =============================================================================
-- These tables represent reusable concepts that exist independently of any
-- specific Data_Asset and are linked via junction tables (created in
-- 0003_junctions.sql). R25.5 — Subject, Instrument, Procedures, Rig are
-- shared records.

-- -----------------------------------------------------------------------------
-- Table: subject
-- -----------------------------------------------------------------------------
-- A living organism (mouse, human, monkey) from which data is collected.
-- subject_id is the external aind-data-schema identifier (R2.6 UNIQUE).
-- date_of_birth is subject to column-level redaction by subject_viewer_v
-- in 0006_rls_policies.sql (R10.3).
CREATE TABLE IF NOT EXISTS subject (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  subject_id               TEXT NOT NULL UNIQUE,
  species                  TEXT NOT NULL,
  sex                      TEXT,
  date_of_birth            DATE,
  genotype                 TEXT,
  source                   TEXT,
  weight_at_acquisition_g  NUMERIC(10, 4),
  age_at_acquisition_days  NUMERIC(10, 2),
  notes                    TEXT,
  metadata                 JSONB NOT NULL DEFAULT '{}'::jsonb,
  embedding                vector(1024),
  description_vec_status   description_vec_status NOT NULL DEFAULT 'pending',
  created_by               UUID NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT subject_dob_not_in_future
    CHECK (date_of_birth IS NULL OR date_of_birth <= CURRENT_DATE),
  CONSTRAINT subject_weight_non_negative
    CHECK (weight_at_acquisition_g IS NULL OR weight_at_acquisition_g >= 0),
  CONSTRAINT subject_age_non_negative
    CHECK (age_at_acquisition_days IS NULL OR age_at_acquisition_days >= 0)
);

CREATE INDEX IF NOT EXISTS subject_species_idx              ON subject (species);
CREATE INDEX IF NOT EXISTS subject_created_by_idx           ON subject (created_by);
CREATE INDEX IF NOT EXISTS subject_desc_vec_pending_idx     ON subject (created_at)
  WHERE description_vec_status = 'pending';

COMMENT ON TABLE  subject                          IS 'Living organism (mouse, human, monkey) from which data is collected. Shared. R2.2, R25.5.';
COMMENT ON COLUMN subject.id                       IS 'UUID primary key.';
COMMENT ON COLUMN subject.subject_id               IS 'External aind-data-schema subject identifier. UNIQUE (R2.6).';
COMMENT ON COLUMN subject.species                  IS 'Species (e.g. ''Mus musculus'', ''Homo sapiens'').';
COMMENT ON COLUMN subject.sex                      IS 'Biological sex (M/F/U).';
COMMENT ON COLUMN subject.date_of_birth            IS 'Date of birth. Redacted by subject_viewer_v for non-data_administrator roles (R10.3).';
COMMENT ON COLUMN subject.genotype                 IS 'Genotype string (e.g. transgenic line).';
COMMENT ON COLUMN subject.source                   IS 'Provider/colony of origin.';
COMMENT ON COLUMN subject.weight_at_acquisition_g  IS 'Subject weight at the time of data acquisition, grams.';
COMMENT ON COLUMN subject.age_at_acquisition_days  IS 'Subject age at the time of data acquisition, days.';
COMMENT ON COLUMN subject.notes                    IS 'Free-text notes.';
COMMENT ON COLUMN subject.metadata                 IS 'JSONB passthrough for non-promoted aind-data-schema fields.';
COMMENT ON COLUMN subject.embedding                IS 'pgvector(1024) embedding for similarity search and duplicate detection (R3.2).';
COMMENT ON COLUMN subject.description_vec_status   IS 'Embedding state — drives Embedding_Backfill_Lambda.';
COMMENT ON COLUMN subject.created_by               IS 'app_user that created the row.';
COMMENT ON COLUMN subject.created_at               IS 'Record creation timestamp (UTC).';


-- -----------------------------------------------------------------------------
-- Table: instrument
-- -----------------------------------------------------------------------------
-- A microscope or hand-built device used to collect data. Shared.
CREATE TABLE IF NOT EXISTS instrument (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  instrument_id            TEXT NOT NULL UNIQUE,
  instrument_type          TEXT,
  manufacturer             TEXT,
  model                    TEXT,
  serial_number            TEXT,
  calibration_date         DATE,
  notes                    TEXT,
  metadata                 JSONB NOT NULL DEFAULT '{}'::jsonb,
  embedding                vector(1024),
  description_vec_status   description_vec_status NOT NULL DEFAULT 'pending',
  created_by               UUID NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT instrument_calibration_not_in_future
    CHECK (calibration_date IS NULL OR calibration_date <= CURRENT_DATE)
);

CREATE INDEX IF NOT EXISTS instrument_type_idx               ON instrument (instrument_type);
CREATE INDEX IF NOT EXISTS instrument_manufacturer_idx       ON instrument (manufacturer);
CREATE INDEX IF NOT EXISTS instrument_created_by_idx         ON instrument (created_by);
CREATE INDEX IF NOT EXISTS instrument_desc_vec_pending_idx   ON instrument (created_at)
  WHERE description_vec_status = 'pending';

COMMENT ON TABLE  instrument                       IS 'Microscope or hand-built device used to collect data. Shared. R2.2, R25.5.';
COMMENT ON COLUMN instrument.id                    IS 'UUID primary key.';
COMMENT ON COLUMN instrument.instrument_id         IS 'External aind-data-schema instrument identifier. UNIQUE (R2.6).';
COMMENT ON COLUMN instrument.instrument_type       IS 'Coarse type (e.g. ''ExA-SPIM'', ''Neuropixels probe'').';
COMMENT ON COLUMN instrument.manufacturer          IS 'Manufacturer name.';
COMMENT ON COLUMN instrument.model                 IS 'Manufacturer model identifier.';
COMMENT ON COLUMN instrument.serial_number         IS 'Manufacturer serial number.';
COMMENT ON COLUMN instrument.calibration_date      IS 'Most recent calibration date.';
COMMENT ON COLUMN instrument.notes                 IS 'Free-text notes.';
COMMENT ON COLUMN instrument.metadata              IS 'JSONB passthrough for non-promoted aind-data-schema fields.';
COMMENT ON COLUMN instrument.embedding             IS 'pgvector(1024) embedding for similarity search.';
COMMENT ON COLUMN instrument.description_vec_status IS 'Embedding state — drives Embedding_Backfill_Lambda.';
COMMENT ON COLUMN instrument.created_by            IS 'app_user that created the row.';
COMMENT ON COLUMN instrument.created_at            IS 'Record creation timestamp (UTC).';


-- -----------------------------------------------------------------------------
-- Table: rig
-- -----------------------------------------------------------------------------
-- The physical setup/configuration of instruments for a specific experiment.
-- modalities is text[] rather than an enum array because the modality set
-- is owned by aind-data-schema (same reasoning as data_asset.data_type).
CREATE TABLE IF NOT EXISTS rig (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  rig_id                   TEXT NOT NULL UNIQUE,
  modalities               TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
  location                 TEXT,
  notes                    TEXT,
  metadata                 JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by               UUID NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS rig_created_by_idx     ON rig (created_by);
-- GIN index for `modalities @> ARRAY[...]` queries from Search_Lambda.
CREATE INDEX IF NOT EXISTS rig_modalities_gin_idx ON rig USING GIN (modalities);

COMMENT ON TABLE  rig                IS 'Physical setup/configuration of instruments for a specific experiment. Shared. R2.2, R25.5.';
COMMENT ON COLUMN rig.id             IS 'UUID primary key.';
COMMENT ON COLUMN rig.rig_id         IS 'External aind-data-schema rig identifier. UNIQUE (R2.6).';
COMMENT ON COLUMN rig.modalities     IS 'Array of modality strings (e.g. {''ephys'',''imaging''}).';
COMMENT ON COLUMN rig.location       IS 'Physical location (room, building).';
COMMENT ON COLUMN rig.notes          IS 'Free-text notes.';
COMMENT ON COLUMN rig.metadata       IS 'JSONB passthrough for non-promoted aind-data-schema fields.';
COMMENT ON COLUMN rig.created_by     IS 'app_user that created the row.';
COMMENT ON COLUMN rig.created_at     IS 'Record creation timestamp (UTC).';


-- -----------------------------------------------------------------------------
-- Table: procedures
-- -----------------------------------------------------------------------------
-- Metadata about procedures performed prior to data acquisition (surgeries,
-- behavior training, tissue staining). One-to-many on subject (R25.2):
-- a single subject may undergo multiple procedures.
CREATE TABLE IF NOT EXISTS procedures (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  subject_id               UUID REFERENCES subject(id) ON DELETE SET NULL,
  surgery_date             DATE,
  protocol                 TEXT,
  performed_by             TEXT,
  notes                    TEXT,
  metadata                 JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by               UUID NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT procedures_surgery_not_in_future
    CHECK (surgery_date IS NULL OR surgery_date <= CURRENT_DATE)
);

CREATE INDEX IF NOT EXISTS procedures_subject_id_idx    ON procedures (subject_id);
CREATE INDEX IF NOT EXISTS procedures_created_by_idx    ON procedures (created_by);

COMMENT ON TABLE  procedures                IS 'Procedures performed on subjects prior to data acquisition. Shared (one-to-many on subject, R25.2).';
COMMENT ON COLUMN procedures.id             IS 'UUID primary key.';
COMMENT ON COLUMN procedures.subject_id     IS 'Subject the procedure was performed on (R25.2 one-to-many).';
COMMENT ON COLUMN procedures.surgery_date   IS 'Date of the procedure.';
COMMENT ON COLUMN procedures.protocol       IS 'Protocol identifier or name.';
COMMENT ON COLUMN procedures.performed_by   IS 'Operator name (free text — does not reference app_user since operators are often non-Registry users).';
COMMENT ON COLUMN procedures.notes          IS 'Free-text notes.';
COMMENT ON COLUMN procedures.metadata       IS 'JSONB passthrough for non-promoted aind-data-schema fields.';
COMMENT ON COLUMN procedures.created_by     IS 'app_user that created the row.';
COMMENT ON COLUMN procedures.created_at     IS 'Record creation timestamp (UTC).';


-- =============================================================================
-- Asset-specific entities
-- =============================================================================
-- Tied directly to a Data_Asset via FK with ON DELETE CASCADE: when an
-- asset is deleted, its session/acquisition/processing/quality_control/
-- data_description rows go with it. R25.5.

-- -----------------------------------------------------------------------------
-- Table: session
-- -----------------------------------------------------------------------------
-- A specific recording session. Ties an acquisition to a subject, an
-- instrument, and a rig configuration.
CREATE TABLE IF NOT EXISTS session (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  data_asset_id            UUID NOT NULL REFERENCES data_asset(id) ON DELETE CASCADE,
  session_id               TEXT,
  session_type             TEXT,
  session_start            TIMESTAMPTZ,
  session_end              TIMESTAMPTZ,
  experimenter             TEXT,
  subject_id               UUID REFERENCES subject(id)    ON DELETE SET NULL,
  instrument_id            UUID REFERENCES instrument(id) ON DELETE SET NULL,
  rig_id                   UUID REFERENCES rig(id)        ON DELETE SET NULL,
  notes                    TEXT,
  metadata                 JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT session_end_after_start
    CHECK (
      session_start IS NULL OR session_end IS NULL OR session_end >= session_start
    )
);

CREATE INDEX IF NOT EXISTS session_data_asset_id_idx ON session (data_asset_id);
CREATE INDEX IF NOT EXISTS session_subject_id_idx    ON session (subject_id)    WHERE subject_id    IS NOT NULL;
CREATE INDEX IF NOT EXISTS session_instrument_id_idx ON session (instrument_id) WHERE instrument_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS session_rig_id_idx        ON session (rig_id)        WHERE rig_id        IS NOT NULL;
CREATE INDEX IF NOT EXISTS session_session_id_idx    ON session (session_id)    WHERE session_id    IS NOT NULL;

COMMENT ON TABLE  session                IS 'Recording session tying an acquisition to subject/instrument/rig. Asset-specific (FK to data_asset with CASCADE). R25.5.';
COMMENT ON COLUMN session.id             IS 'UUID primary key.';
COMMENT ON COLUMN session.data_asset_id  IS 'Owning Data_Asset. ON DELETE CASCADE.';
COMMENT ON COLUMN session.session_id     IS 'External aind-data-schema session identifier (not unique — multiple revisions possible).';
COMMENT ON COLUMN session.session_type   IS 'Session type label (e.g. ''behavior'', ''ephys'').';
COMMENT ON COLUMN session.session_start  IS 'Session start timestamp (UTC).';
COMMENT ON COLUMN session.session_end    IS 'Session end timestamp (UTC). Must be >= session_start when both are set.';
COMMENT ON COLUMN session.experimenter   IS 'Experimenter name (free text — does not reference app_user).';
COMMENT ON COLUMN session.subject_id     IS 'Subject recorded in this session.';
COMMENT ON COLUMN session.instrument_id  IS 'Primary instrument used.';
COMMENT ON COLUMN session.rig_id         IS 'Rig configuration used.';
COMMENT ON COLUMN session.notes          IS 'Free-text notes.';
COMMENT ON COLUMN session.metadata       IS 'JSONB passthrough for non-promoted aind-data-schema fields.';
COMMENT ON COLUMN session.created_at     IS 'Record creation timestamp (UTC).';


-- -----------------------------------------------------------------------------
-- Table: acquisition
-- -----------------------------------------------------------------------------
-- A specific data collection capturing instrument configuration at the
-- time of collection. Tied to a session.
CREATE TABLE IF NOT EXISTS acquisition (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  data_asset_id            UUID NOT NULL REFERENCES data_asset(id) ON DELETE CASCADE,
  session_id               UUID REFERENCES session(id)    ON DELETE SET NULL,
  instrument_id            UUID REFERENCES instrument(id) ON DELETE SET NULL,
  acquisition_start        TIMESTAMPTZ,
  acquisition_end          TIMESTAMPTZ,
  parameters               JSONB NOT NULL DEFAULT '{}'::jsonb,
  notes                    TEXT,
  metadata                 JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT acquisition_end_after_start
    CHECK (
      acquisition_start IS NULL OR acquisition_end IS NULL OR acquisition_end >= acquisition_start
    )
);

CREATE INDEX IF NOT EXISTS acquisition_data_asset_id_idx ON acquisition (data_asset_id);
CREATE INDEX IF NOT EXISTS acquisition_session_id_idx    ON acquisition (session_id)    WHERE session_id    IS NOT NULL;
CREATE INDEX IF NOT EXISTS acquisition_instrument_id_idx ON acquisition (instrument_id) WHERE instrument_id IS NOT NULL;

COMMENT ON TABLE  acquisition                    IS 'Data collection event capturing instrument configuration at time of collection. Asset-specific. R25.5.';
COMMENT ON COLUMN acquisition.id                 IS 'UUID primary key.';
COMMENT ON COLUMN acquisition.data_asset_id      IS 'Owning Data_Asset. ON DELETE CASCADE.';
COMMENT ON COLUMN acquisition.session_id         IS 'Session this acquisition belongs to.';
COMMENT ON COLUMN acquisition.instrument_id      IS 'Instrument used.';
COMMENT ON COLUMN acquisition.acquisition_start  IS 'Acquisition start timestamp (UTC).';
COMMENT ON COLUMN acquisition.acquisition_end    IS 'Acquisition end timestamp (UTC). Must be >= acquisition_start when both are set.';
COMMENT ON COLUMN acquisition.parameters         IS 'JSONB instrument parameters captured at acquisition time.';
COMMENT ON COLUMN acquisition.notes              IS 'Free-text notes.';
COMMENT ON COLUMN acquisition.metadata           IS 'JSONB passthrough for non-promoted aind-data-schema fields.';
COMMENT ON COLUMN acquisition.created_at         IS 'Record creation timestamp (UTC).';


-- -----------------------------------------------------------------------------
-- Table: processing
-- -----------------------------------------------------------------------------
-- A computational step applied to data (compression, format conversion,
-- registration). Documents provenance.
CREATE TABLE IF NOT EXISTS processing (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  data_asset_id            UUID NOT NULL REFERENCES data_asset(id) ON DELETE CASCADE,
  processing_pipeline      TEXT,
  version                  TEXT,
  parameters               JSONB NOT NULL DEFAULT '{}'::jsonb,
  notes                    TEXT,
  started_at               TIMESTAMPTZ,
  completed_at             TIMESTAMPTZ,
  metadata                 JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT processing_completed_after_started
    CHECK (
      started_at IS NULL OR completed_at IS NULL OR completed_at >= started_at
    )
);

CREATE INDEX IF NOT EXISTS processing_data_asset_id_idx       ON processing (data_asset_id);
CREATE INDEX IF NOT EXISTS processing_processing_pipeline_idx ON processing (processing_pipeline)
  WHERE processing_pipeline IS NOT NULL;

COMMENT ON TABLE  processing                       IS 'Computational step applied to a Data_Asset. Documents provenance. Asset-specific. R25.5.';
COMMENT ON COLUMN processing.id                    IS 'UUID primary key.';
COMMENT ON COLUMN processing.data_asset_id         IS 'Owning Data_Asset. ON DELETE CASCADE.';
COMMENT ON COLUMN processing.processing_pipeline   IS 'Pipeline name (e.g. ''aind-ephys-pipeline'').';
COMMENT ON COLUMN processing.version               IS 'Pipeline version string.';
COMMENT ON COLUMN processing.parameters            IS 'JSONB pipeline parameters.';
COMMENT ON COLUMN processing.notes                 IS 'Free-text notes.';
COMMENT ON COLUMN processing.started_at            IS 'Pipeline start timestamp (UTC).';
COMMENT ON COLUMN processing.completed_at          IS 'Pipeline completion timestamp (UTC). Must be >= started_at when both are set.';
COMMENT ON COLUMN processing.metadata              IS 'JSONB passthrough for non-promoted aind-data-schema fields.';
COMMENT ON COLUMN processing.created_at            IS 'Record creation timestamp (UTC).';


-- -----------------------------------------------------------------------------
-- Table: quality_control
-- -----------------------------------------------------------------------------
-- Annotations on data quality (e.g. "last 30 minutes of video corrupted").
CREATE TABLE IF NOT EXISTS quality_control (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  data_asset_id            UUID NOT NULL REFERENCES data_asset(id) ON DELETE CASCADE,
  qc_metric                TEXT,
  value                    NUMERIC,
  unit                     TEXT,
  status                   TEXT,
  notes                    TEXT,
  metadata                 JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS quality_control_data_asset_id_idx ON quality_control (data_asset_id);
CREATE INDEX IF NOT EXISTS quality_control_qc_metric_idx     ON quality_control (qc_metric) WHERE qc_metric IS NOT NULL;
CREATE INDEX IF NOT EXISTS quality_control_status_idx        ON quality_control (status)    WHERE status    IS NOT NULL;

COMMENT ON TABLE  quality_control                IS 'Annotations on data quality. Asset-specific. R25.5.';
COMMENT ON COLUMN quality_control.id             IS 'UUID primary key.';
COMMENT ON COLUMN quality_control.data_asset_id  IS 'Owning Data_Asset. ON DELETE CASCADE.';
COMMENT ON COLUMN quality_control.qc_metric      IS 'Metric name (e.g. ''snr'', ''dropped_frames'').';
COMMENT ON COLUMN quality_control.value          IS 'Numeric metric value.';
COMMENT ON COLUMN quality_control.unit           IS 'Unit string (e.g. ''dB'').';
COMMENT ON COLUMN quality_control.status         IS 'Pass/warn/fail or other free-form status label.';
COMMENT ON COLUMN quality_control.notes          IS 'Free-text notes describing the QC observation.';
COMMENT ON COLUMN quality_control.metadata       IS 'JSONB passthrough for non-promoted aind-data-schema fields.';
COMMENT ON COLUMN quality_control.created_at     IS 'Record creation timestamp (UTC).';


-- -----------------------------------------------------------------------------
-- Table: data_description
-- -----------------------------------------------------------------------------
-- Administrative metadata about a Data_Asset including funding source,
-- licenses, and restrictions on use. May be human-authored or auto-generated
-- by the MetaData_Agent (description_kind enum).
CREATE TABLE IF NOT EXISTS data_description (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  data_asset_id            UUID NOT NULL REFERENCES data_asset(id) ON DELETE CASCADE,
  description_kind         description_kind NOT NULL DEFAULT 'human',
  text                     TEXT,
  language                 TEXT NOT NULL DEFAULT 'en',
  funding_source           TEXT,
  license                  TEXT,
  metadata                 JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS data_description_data_asset_id_idx     ON data_description (data_asset_id);
CREATE INDEX IF NOT EXISTS data_description_description_kind_idx  ON data_description (description_kind);

COMMENT ON TABLE  data_description                    IS 'Administrative metadata: funding, licenses, descriptions. Asset-specific. R25.5.';
COMMENT ON COLUMN data_description.id                 IS 'UUID primary key.';
COMMENT ON COLUMN data_description.data_asset_id      IS 'Owning Data_Asset. ON DELETE CASCADE.';
COMMENT ON COLUMN data_description.description_kind   IS '''human'' for user-authored, ''auto'' for agent-generated.';
COMMENT ON COLUMN data_description.text               IS 'Description text (natural language).';
COMMENT ON COLUMN data_description.language           IS 'BCP-47 language tag for ''text''. Defaults to ''en''.';
COMMENT ON COLUMN data_description.funding_source     IS 'Funding source (e.g. NIH grant number).';
COMMENT ON COLUMN data_description.license            IS 'License identifier (e.g. ''CC-BY-4.0'').';
COMMENT ON COLUMN data_description.metadata           IS 'JSONB passthrough for non-promoted aind-data-schema fields.';
COMMENT ON COLUMN data_description.created_at         IS 'Record creation timestamp (UTC).';

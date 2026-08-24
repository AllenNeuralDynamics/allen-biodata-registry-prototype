-- Allen BioData Registry — Schema (excerpt)
-- The full migrations live in `migrations/0001_*.sql` through `0011_*.sql`.
-- This file is the reduced, KB-friendly view of the schema with the columns
-- and constraints most relevant to NL→SQL generation.

-- =============================================================================
-- Governance
-- =============================================================================

CREATE TABLE organization (
  id           UUID PRIMARY KEY,
  name         TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE space (
  id           UUID PRIMARY KEY,
  org_id       UUID NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  name         TEXT NOT NULL,
  display_name TEXT NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (org_id, name)
);

CREATE TABLE app_user (
  id            UUID PRIMARY KEY,
  cognito_sub   TEXT UNIQUE,
  email         TEXT NOT NULL,
  display_name  TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Roles: data_administrator, org_admin, space_admin, contributor, viewer.

-- =============================================================================
-- Core data
-- =============================================================================

CREATE TABLE data_asset (
  id                 UUID PRIMARY KEY,
  name               TEXT NOT NULL,
  storage_uri        TEXT NOT NULL UNIQUE,    -- e.g. s3://bucket/key
  data_type          TEXT NOT NULL,           -- modality: behavior, ephys, ophys, fmri, ...
  org_id             UUID NOT NULL REFERENCES organization(id),
  space_id           UUID NOT NULL REFERENCES space(id),
  schema_id          UUID REFERENCES schema_definition(id),
  validation_status  TEXT NOT NULL,           -- valid | invalid | pending | schema-deprecated
  validation_errors  JSONB,
  lifecycle_state    TEXT NOT NULL,           -- draft | registered | published | archived
  is_sensitive       BOOLEAN NOT NULL DEFAULT false,
  metadata           JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Shared entities (referenced by many data_asset rows via junction tables)

CREATE TABLE subject (
  id              UUID PRIMARY KEY,
  subject_id      TEXT NOT NULL,              -- lab-assigned identifier
  species         TEXT,                       -- e.g. "mus musculus"
  sex             TEXT,                       -- M | F | U
  date_of_birth   DATE,                       -- COLUMN-LEVEL VIEW REDACTS THIS for non-privileged users
  genotype        TEXT,
  metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
  embedding       vector(1024),               -- pgvector — populated by Embedding_Backfill_Lambda
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE instrument (
  id            UUID PRIMARY KEY,
  instrument_id TEXT NOT NULL,
  name          TEXT,
  manufacturer  TEXT,
  model         TEXT,
  metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,
  embedding     vector(1024),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE data_asset_subject (
  data_asset_id UUID REFERENCES data_asset(id) ON DELETE CASCADE,
  subject_id    UUID REFERENCES subject(id) ON DELETE RESTRICT,
  PRIMARY KEY (data_asset_id, subject_id)
);

-- =============================================================================
-- Audit / governance trail
-- =============================================================================

CREATE TABLE entity_revision (
  id                UUID PRIMARY KEY,
  entity_type       TEXT NOT NULL,
  entity_id         UUID NOT NULL,
  revision_number   INT  NOT NULL,
  change_source     TEXT NOT NULL,            -- manual | agent | ETL | merge | system
  metadata_snapshot JSONB,
  user_id           UUID REFERENCES app_user(id),
  "timestamp"       TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (entity_type, entity_id, revision_number)
);
-- entity_revision is immutable: the biodata_app role has SELECT + INSERT only.
-- UPDATE and DELETE are revoked by migration 0004 — the audit trail cannot be
-- forged.

CREATE TABLE lifecycle_transition (
  id              UUID PRIMARY KEY,
  data_asset_id   UUID NOT NULL REFERENCES data_asset(id),
  previous_state  TEXT NOT NULL,
  new_state       TEXT NOT NULL,
  user_id         UUID REFERENCES app_user(id),
  "timestamp"     TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (previous_state <> new_state)
);

CREATE TABLE duplicate_flag (
  id               UUID PRIMARY KEY,
  entity_type      TEXT NOT NULL,
  entity_a_id      UUID NOT NULL,
  entity_b_id      UUID NOT NULL,
  similarity_score NUMERIC NOT NULL,           -- in [0, 1]
  flagged_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  dismissed        BOOLEAN NOT NULL DEFAULT false,
  dismissed_by     UUID REFERENCES app_user(id),
  dismissed_at     TIMESTAMPTZ,
  UNIQUE (entity_type, entity_a_id, entity_b_id),
  CHECK (entity_a_id <> entity_b_id),
  CHECK (similarity_score BETWEEN 0 AND 1)
);

-- =============================================================================
-- RLS context
-- =============================================================================
-- Every connection sets `app.current_user_id`, `app.current_space_ids`,
-- `app.current_org_ids`, `app.current_roles` via SET LOCAL. Policies on
-- data_asset, subject, entity_revision and other tables filter by these.
-- See migrations/0006_rls_policies.sql for the full policy set.

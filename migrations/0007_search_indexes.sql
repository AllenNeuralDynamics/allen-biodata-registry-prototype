-- =============================================================================
-- Migration: 0007_search_indexes.sql
-- Purpose:   Create the Aurora-side search indexes that back the
--            Search_Lambda fallback path (R17.12) and the pgvector ivfflat
--            indexes used for similarity-based duplicate detection
--            (Duplicates_Lambda) and hybrid lexical+vector search when
--            OpenSearch is unavailable.
--
-- Spec:      .kiro/specs/allen-biodata-registry-poc
-- Task:      7.7 — Create migration 0007_search_indexes.sql
-- Validates: R17.12
-- Design:    §Components.7. Search_Lambda (fallback)
--            §Error Handling.Failure Domains
--            §Architecture (read paths)
--
-- Idempotency:
--   * Every column add uses ALTER TABLE ... ADD COLUMN IF NOT EXISTS, which
--     short-circuits silently when the generated tsvector column already
--     exists. The generation expression is therefore *committed once* —
--     changing it later requires an explicit DROP/ADD migration.
--   * Every index uses CREATE INDEX IF NOT EXISTS.
--   * The pgvector-detection DO block uses a guarded CREATE so re-runs are
--     a no-op whether or not the operator class is present.
--   * Re-running this migration after a successful first run is a no-op.
--
-- Ordering / runner contract:
--   * The migration runner (Task 8.1) applies *.sql files in lexical order
--     inside a single transaction. ivfflat indexes can be created inside a
--     transaction (unlike CREATE INDEX CONCURRENTLY), so this migration
--     does NOT need the `-- +runner: no-transaction` directive declared in
--     migrations/README.md. The brief ACCESS EXCLUSIVE lock taken while
--     each ivfflat index builds is acceptable for the PoC; production
--     re-indexing would use CONCURRENTLY in a one-off script.
--   * Depends on tables created by 0002_data_asset.sql (data_asset,
--     subject, instrument, rig, procedures, session, acquisition,
--     processing, quality_control, data_description). 0001/0003/0004/0005
--     are also expected to have applied before 0007 but this migration
--     itself only touches tables defined in 0002.
--
-- =============================================================================
-- Why generated tsvector columns instead of expression indexes?
-- =============================================================================
-- Two equivalent ways to back a tsvector index in PostgreSQL:
--
--   1. Expression index:
--        CREATE INDEX ... ON foo USING GIN (to_tsvector('english', col));
--      Pros: smaller schema surface.
--      Cons: every query must repeat the same to_tsvector(...) expression
--            verbatim to use the index, including in the Search_Lambda
--            fallback path; a query writing `to_tsvector('english', col)
--            @@ ...` matches but `plainto_tsquery(col) @@ ...` does not.
--            Easy to silently mis-route queries off the index.
--
--   2. Generated STORED column + plain GIN index (THIS MIGRATION):
--        ALTER TABLE foo ADD COLUMN search_vec tsvector
--          GENERATED ALWAYS AS (to_tsvector('english', ...)) STORED;
--        CREATE INDEX ... ON foo USING GIN (search_vec);
--      Pros: a *column*, not an expression — Search_Lambda's fallback
--            query is the trivial `WHERE search_vec @@ websearch_to_tsquery(...)`
--            which always uses the index. CDC also exposes the column to
--            DocumentDB if we ever want to project it.
--      Cons: ~10–20% larger row footprint (one tsvector per row).
--
-- For a fallback path that only runs when OpenSearch is degraded, query
-- correctness > storage. Generated columns are also surfaced as plain
-- columns in pg_attribute / information_schema, which makes the schema
-- self-documenting via `\d data_asset`.
--
-- All generation expressions use the two-argument `to_tsvector(regconfig,
-- text)` form, which is IMMUTABLE (the one-argument form is STABLE and
-- would be rejected for a STORED generated column). `coalesce`, `||`,
-- `array_to_string`, and the JSONB::text / CITEXT::text casts used below
-- are all IMMUTABLE.
--
-- =============================================================================
-- Why ivfflat with lists = 100, and what to re-tune later
-- =============================================================================
-- pgvector offers two index types: ivfflat and hnsw. ivfflat is chosen for
-- the PoC because:
--   * Build time is O(n) vs hnsw's O(n log n), which matters for the
--     7GB seed reload.
--   * lists is the only knob and rule-of-thumb is well-published:
--       lists ≈ rows / 1000  for ≤ 1M rows
--       lists ≈ sqrt(rows)   for > 1M rows
--     The customer's PoC seed is ~10K assets / ~10K subjects / a few
--     hundred instruments — so lists = 100 is the smallest reasonable
--     value that still partitions the space (one centroid per ~100 rows).
--   * ivfflat tolerates *small* corpora — the index simply degrades to
--     near-flat scan when there are fewer rows than lists * 1000, which
--     is acceptable. It does NOT error or refuse to build.
--
-- IMPORTANT for the Allen Institute operator:
-- After the seeder (Task 9.1) loads its 10% of the 7GB JSON snapshot,
-- run REINDEX on the three ivfflat indexes named below to build proper
-- centroids on real data:
--
--   REINDEX INDEX CONCURRENTLY data_asset_description_vec_ivfflat;
--   REINDEX INDEX CONCURRENTLY subject_embedding_ivfflat;
--   REINDEX INDEX CONCURRENTLY instrument_embedding_ivfflat;
--
-- If row counts move past 1M in a given table, recreate that index with
-- lists ≈ sqrt(row_count) and probes ≈ sqrt(lists) at query time.
--
-- =============================================================================
-- pgvector availability — local-lint shim
-- =============================================================================
-- 0002_data_asset.sql falls back to `CREATE DOMAIN vector AS text` when the
-- pgvector extension is unavailable (for stock-Postgres lint runs). In that
-- environment the ivfflat operator classes do not exist, so the
-- `CREATE INDEX ... USING ivfflat (... vector_cosine_ops)` calls would
-- fail. This migration therefore guards the ivfflat blocks with a probe
-- against pg_opclass so:
--   * On Aurora (pgvector installed): all three ivfflat indexes are
--     created.
--   * On local lint (pgvector absent): the ivfflat block is skipped with
--     a RAISE NOTICE so the lint run succeeds and the operator is told
--     why.
-- The tsvector columns and GIN indexes are unaffected — those are core
-- PostgreSQL features.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. Generated tsvector columns
-- -----------------------------------------------------------------------------
-- One generated column per table that backs the Search_Lambda fallback.
-- The column name `search_vec` is consistent across tables so the fallback
-- query in design.md §Components.7 can be templated by entity type.
-- Each generation expression coalesces the relevant searchable text fields
-- and casts JSONB-style fields to text so unstructured aind-data-schema
-- passthrough metadata is also indexed.

-- ........................ data_asset ........................
ALTER TABLE data_asset
  ADD COLUMN IF NOT EXISTS search_vec tsvector
  GENERATED ALWAYS AS (
    to_tsvector(
      'english',
      coalesce(name::text, '')        || ' ' ||
      coalesce(display_name, '')      || ' ' ||
      coalesce(description, '')       || ' ' ||
      coalesce(data_type, '')         || ' ' ||
      coalesce(storage_uri, '')       || ' ' ||
      coalesce(metadata::text, '')
    )
  ) STORED;

COMMENT ON COLUMN data_asset.search_vec IS
  'Generated STORED tsvector over name, display_name, description, data_type, storage_uri, and metadata. Used by Search_Lambda fallback when OpenSearch is unavailable (R17.12). Index: data_asset_search_vec_gin.';


-- ........................ subject ........................
ALTER TABLE subject
  ADD COLUMN IF NOT EXISTS search_vec tsvector
  GENERATED ALWAYS AS (
    to_tsvector(
      'english',
      coalesce(subject_id, '')   || ' ' ||
      coalesce(species, '')      || ' ' ||
      coalesce(sex, '')          || ' ' ||
      coalesce(genotype, '')     || ' ' ||
      coalesce(source, '')       || ' ' ||
      coalesce(notes, '')        || ' ' ||
      coalesce(metadata::text, '')
    )
  ) STORED;

COMMENT ON COLUMN subject.search_vec IS
  'Generated STORED tsvector over subject_id, species, sex, genotype, source, notes, and metadata. Backs Search_Lambda fallback (R17.12). Index: subject_search_vec_gin.';


-- ........................ instrument ........................
ALTER TABLE instrument
  ADD COLUMN IF NOT EXISTS search_vec tsvector
  GENERATED ALWAYS AS (
    to_tsvector(
      'english',
      coalesce(instrument_id, '')   || ' ' ||
      coalesce(instrument_type, '') || ' ' ||
      coalesce(manufacturer, '')    || ' ' ||
      coalesce(model, '')           || ' ' ||
      coalesce(serial_number, '')   || ' ' ||
      coalesce(notes, '')           || ' ' ||
      coalesce(metadata::text, '')
    )
  ) STORED;

COMMENT ON COLUMN instrument.search_vec IS
  'Generated STORED tsvector over instrument_id, instrument_type, manufacturer, model, serial_number, notes, and metadata. Backs Search_Lambda fallback (R17.12). Index: instrument_search_vec_gin.';


-- ........................ rig ........................
-- NOTE on modalities (text[]): `array_to_string(arr, ' ')` and the
-- equivalent `arr::text` cast are both STABLE in PostgreSQL 16, not
-- IMMUTABLE — they cannot appear in a STORED generated column. The
-- tsvector therefore intentionally omits modalities. Modality faceting
-- already has dedicated index coverage:
--   * rig_modalities_gin_idx (created in 0002_data_asset.sql) supports
--     `modalities @> ARRAY[...]` exact-set queries.
--   * The OpenSearch index keeps modalities as a `keyword` array, which
--     is the primary path; the Aurora fallback only needs to support
--     full-text queries against free-form fields (rig_id, location,
--     notes), which it does.
ALTER TABLE rig
  ADD COLUMN IF NOT EXISTS search_vec tsvector
  GENERATED ALWAYS AS (
    to_tsvector(
      'english',
      coalesce(rig_id, '')        || ' ' ||
      coalesce(location, '')      || ' ' ||
      coalesce(notes, '')         || ' ' ||
      coalesce(metadata::text, '')
    )
  ) STORED;

COMMENT ON COLUMN rig.search_vec IS
  'Generated STORED tsvector over rig_id, location, notes, and metadata. Modalities are intentionally excluded (text[] has no IMMUTABLE serializer in PG16); use rig_modalities_gin_idx for modality faceting. Backs Search_Lambda fallback (R17.12). Index: rig_search_vec_gin.';


-- ........................ procedures ........................
ALTER TABLE procedures
  ADD COLUMN IF NOT EXISTS search_vec tsvector
  GENERATED ALWAYS AS (
    to_tsvector(
      'english',
      coalesce(protocol, '')      || ' ' ||
      coalesce(performed_by, '')  || ' ' ||
      coalesce(notes, '')         || ' ' ||
      coalesce(metadata::text, '')
    )
  ) STORED;

COMMENT ON COLUMN procedures.search_vec IS
  'Generated STORED tsvector over protocol, performed_by, notes, and metadata. Backs Search_Lambda fallback (R17.12). Index: procedures_search_vec_gin.';


-- ........................ session ........................
ALTER TABLE session
  ADD COLUMN IF NOT EXISTS search_vec tsvector
  GENERATED ALWAYS AS (
    to_tsvector(
      'english',
      coalesce(session_id, '')    || ' ' ||
      coalesce(session_type, '')  || ' ' ||
      coalesce(experimenter, '')  || ' ' ||
      coalesce(notes, '')         || ' ' ||
      coalesce(metadata::text, '')
    )
  ) STORED;

COMMENT ON COLUMN session.search_vec IS
  'Generated STORED tsvector over session_id, session_type, experimenter, notes, and metadata. Backs Search_Lambda fallback (R17.12). Index: session_search_vec_gin.';


-- ........................ acquisition ........................
-- parameters is JSONB and frequently contains free-text instrument
-- settings (channel names, gain labels) that researchers search by, so it
-- is included alongside notes and metadata.
ALTER TABLE acquisition
  ADD COLUMN IF NOT EXISTS search_vec tsvector
  GENERATED ALWAYS AS (
    to_tsvector(
      'english',
      coalesce(parameters::text, '') || ' ' ||
      coalesce(notes, '')            || ' ' ||
      coalesce(metadata::text, '')
    )
  ) STORED;

COMMENT ON COLUMN acquisition.search_vec IS
  'Generated STORED tsvector over parameters, notes, and metadata. Backs Search_Lambda fallback (R17.12). Index: acquisition_search_vec_gin.';


-- ........................ processing ........................
ALTER TABLE processing
  ADD COLUMN IF NOT EXISTS search_vec tsvector
  GENERATED ALWAYS AS (
    to_tsvector(
      'english',
      coalesce(processing_pipeline, '') || ' ' ||
      coalesce(version, '')             || ' ' ||
      coalesce(parameters::text, '')    || ' ' ||
      coalesce(notes, '')               || ' ' ||
      coalesce(metadata::text, '')
    )
  ) STORED;

COMMENT ON COLUMN processing.search_vec IS
  'Generated STORED tsvector over processing_pipeline, version, parameters, notes, and metadata. Backs Search_Lambda fallback (R17.12). Index: processing_search_vec_gin.';


-- ........................ quality_control ........................
ALTER TABLE quality_control
  ADD COLUMN IF NOT EXISTS search_vec tsvector
  GENERATED ALWAYS AS (
    to_tsvector(
      'english',
      coalesce(qc_metric, '')      || ' ' ||
      coalesce(unit, '')           || ' ' ||
      coalesce(status, '')         || ' ' ||
      coalesce(notes, '')          || ' ' ||
      coalesce(metadata::text, '')
    )
  ) STORED;

COMMENT ON COLUMN quality_control.search_vec IS
  'Generated STORED tsvector over qc_metric, unit, status, notes, and metadata. Backs Search_Lambda fallback (R17.12). Index: quality_control_search_vec_gin.';


-- ........................ data_description ........................
ALTER TABLE data_description
  ADD COLUMN IF NOT EXISTS search_vec tsvector
  GENERATED ALWAYS AS (
    to_tsvector(
      'english',
      coalesce(text, '')           || ' ' ||
      coalesce(funding_source, '') || ' ' ||
      coalesce(license, '')        || ' ' ||
      coalesce(metadata::text, '')
    )
  ) STORED;

COMMENT ON COLUMN data_description.search_vec IS
  'Generated STORED tsvector over text, funding_source, license, and metadata. Backs Search_Lambda fallback (R17.12). Index: data_description_search_vec_gin.';


-- -----------------------------------------------------------------------------
-- 2. GIN indexes on the generated tsvector columns
-- -----------------------------------------------------------------------------
-- One GIN index per generated column. GIN is the standard PostgreSQL
-- inverted index for tsvector and is what `@@ websearch_to_tsquery(...)`
-- expects. Naming: <table>_search_vec_gin so it is self-describing in
-- pg_indexes / EXPLAIN output.

CREATE INDEX IF NOT EXISTS data_asset_search_vec_gin
  ON data_asset USING GIN (search_vec);
COMMENT ON INDEX data_asset_search_vec_gin IS
  'GIN index over data_asset.search_vec. Backs Search_Lambda fallback for full-text queries against assets when OpenSearch is degraded (R17.12).';

CREATE INDEX IF NOT EXISTS subject_search_vec_gin
  ON subject USING GIN (search_vec);
COMMENT ON INDEX subject_search_vec_gin IS
  'GIN index over subject.search_vec. Backs Search_Lambda fallback for full-text queries against subjects when OpenSearch is degraded (R17.12).';

CREATE INDEX IF NOT EXISTS instrument_search_vec_gin
  ON instrument USING GIN (search_vec);
COMMENT ON INDEX instrument_search_vec_gin IS
  'GIN index over instrument.search_vec. Backs Search_Lambda fallback for full-text queries against instruments when OpenSearch is degraded (R17.12).';

CREATE INDEX IF NOT EXISTS rig_search_vec_gin
  ON rig USING GIN (search_vec);
COMMENT ON INDEX rig_search_vec_gin IS
  'GIN index over rig.search_vec. Backs Search_Lambda fallback for full-text queries against rigs when OpenSearch is degraded (R17.12).';

CREATE INDEX IF NOT EXISTS procedures_search_vec_gin
  ON procedures USING GIN (search_vec);
COMMENT ON INDEX procedures_search_vec_gin IS
  'GIN index over procedures.search_vec. Backs Search_Lambda fallback for full-text queries against procedures when OpenSearch is degraded (R17.12).';

CREATE INDEX IF NOT EXISTS session_search_vec_gin
  ON session USING GIN (search_vec);
COMMENT ON INDEX session_search_vec_gin IS
  'GIN index over session.search_vec. Backs Search_Lambda fallback for full-text queries against sessions when OpenSearch is degraded (R17.12).';

CREATE INDEX IF NOT EXISTS acquisition_search_vec_gin
  ON acquisition USING GIN (search_vec);
COMMENT ON INDEX acquisition_search_vec_gin IS
  'GIN index over acquisition.search_vec. Backs Search_Lambda fallback for full-text queries against acquisitions when OpenSearch is degraded (R17.12).';

CREATE INDEX IF NOT EXISTS processing_search_vec_gin
  ON processing USING GIN (search_vec);
COMMENT ON INDEX processing_search_vec_gin IS
  'GIN index over processing.search_vec. Backs Search_Lambda fallback for full-text queries against processing rows when OpenSearch is degraded (R17.12).';

CREATE INDEX IF NOT EXISTS quality_control_search_vec_gin
  ON quality_control USING GIN (search_vec);
COMMENT ON INDEX quality_control_search_vec_gin IS
  'GIN index over quality_control.search_vec. Backs Search_Lambda fallback for full-text queries against QC rows when OpenSearch is degraded (R17.12).';

CREATE INDEX IF NOT EXISTS data_description_search_vec_gin
  ON data_description USING GIN (search_vec);
COMMENT ON INDEX data_description_search_vec_gin IS
  'GIN index over data_description.search_vec. Backs Search_Lambda fallback for full-text queries against data_description rows when OpenSearch is degraded (R17.12).';


-- -----------------------------------------------------------------------------
-- 3. ivfflat pgvector indexes
-- -----------------------------------------------------------------------------
-- Created only when the pgvector extension is actually installed (i.e.
-- vector_cosine_ops exists in pg_opclass). On the local-lint stock-Postgres
-- environment this block is a NOTICE-only no-op.
--
-- All three indexes use vector_cosine_ops because:
--   * Embedding_Backfill_Lambda emits L2-normalized embeddings (Bedrock
--     Titan / similar), so cosine == 1 - dot product on normalized
--     vectors. Cosine is also what OpenSearch's knn_vector(1024, "cosine")
--     mapping uses (design.md §Data Models.OpenSearch), keeping the
--     fallback consistent with the primary path.
--   * Duplicates_Lambda computes "cosine similarity" by name in
--     design.md §Components.5; matching the operator class avoids a
--     surprise mismatch between SQL-level expectation and physical index.

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM pg_opclass
    WHERE opcname = 'vector_cosine_ops'
  ) THEN
    -- data_asset.description_vec — backs hybrid lexical+semantic search
    -- and similar-asset duplicate detection (R3.2, R26.1).
    EXECUTE $ddl$
      CREATE INDEX IF NOT EXISTS data_asset_description_vec_ivfflat
        ON data_asset USING ivfflat (description_vec vector_cosine_ops)
        WITH (lists = 100)
    $ddl$;

    EXECUTE $ddl$
      COMMENT ON INDEX data_asset_description_vec_ivfflat IS
        'ivfflat (cosine) over data_asset.description_vec, lists=100. Used by Duplicates_Lambda for similar-asset detection (R3.2, R26.1) and by Search_Lambda hybrid search fallback. REINDEX after seed loads >>1k rows.'
    $ddl$;

    -- subject.embedding — backs subject-level duplicate detection across
    -- studies (R3.2: "same subject re-registered with slightly different
    -- IDs").
    EXECUTE $ddl$
      CREATE INDEX IF NOT EXISTS subject_embedding_ivfflat
        ON subject USING ivfflat (embedding vector_cosine_ops)
        WITH (lists = 100)
    $ddl$;

    EXECUTE $ddl$
      COMMENT ON INDEX subject_embedding_ivfflat IS
        'ivfflat (cosine) over subject.embedding, lists=100. Used by Duplicates_Lambda for cross-study subject collisions (R3.2). REINDEX after seed loads >>1k rows.'
    $ddl$;

    -- instrument.embedding — backs instrument duplicate detection
    -- (same physical microscope re-registered with a typo, etc.).
    EXECUTE $ddl$
      CREATE INDEX IF NOT EXISTS instrument_embedding_ivfflat
        ON instrument USING ivfflat (embedding vector_cosine_ops)
        WITH (lists = 100)
    $ddl$;

    EXECUTE $ddl$
      COMMENT ON INDEX instrument_embedding_ivfflat IS
        'ivfflat (cosine) over instrument.embedding, lists=100. Used by Duplicates_Lambda for instrument collisions (R3.2). REINDEX after seed loads >>1k rows.'
    $ddl$;

    RAISE NOTICE 'pgvector ivfflat indexes created (lists=100). REINDEX after seeder loads real corpus.';
  ELSE
    RAISE NOTICE 'pgvector vector_cosine_ops not found; skipping ivfflat index creation. Production Aurora has the extension installed before this migration runs.';
  END IF;
END;
$$;


-- =============================================================================
-- End of 0007_search_indexes.sql
-- =============================================================================

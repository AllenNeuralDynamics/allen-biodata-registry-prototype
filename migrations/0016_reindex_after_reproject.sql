-- =============================================================================
-- Migration: 0016_reindex_after_reproject.sql
-- Purpose:   Force a full OpenSearch/DocumentDB re-index AFTER 0015 re-projected
--            real species/data_type onto the rows. A no-op touch bumps
--            updated_at on every data_asset, emitting one WAL change per row;
--            the cdc-reader drains them and the Indexing_Lambda re-hydrates
--            each asset (JOINing the now-corrected subject.species and reading
--            the now-populated data_asset.data_type) and overwrites the
--            OpenSearch doc. Idempotent; touches updated_at only.
-- =============================================================================

UPDATE data_asset SET updated_at = now();

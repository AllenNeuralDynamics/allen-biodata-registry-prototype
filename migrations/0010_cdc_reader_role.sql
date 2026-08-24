-- ============================================================================
-- Migration 0010 — cdc_reader role
--
-- The cdc-pipeline Terraform module's cdc-reader Lambda authenticates as
-- 'cdc_reader' (set by the module's hard-coded env var DB_USER). We
-- create that role here with rds_replication so it can drain logical
-- replication slots, plus rds_iam so it can authenticate via the
-- Aurora IAM auth path.
--
-- Note: this is *separate* from cdc_indexer (created in 0008) which is
-- used by the Indexing Lambda for hydrating event payloads. cdc_reader
-- only reads the slot; cdc_indexer reads the registry tables.
--
-- Validates: R28.1, R28.2.
-- ============================================================================

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cdc_reader') THEN
    CREATE ROLE cdc_reader WITH LOGIN;
    COMMENT ON ROLE cdc_reader IS
      'CDC reader Lambda. rds_replication for logical slot reads; rds_iam for '
      'IAM authentication. Does NOT need BYPASSRLS — only reads from the slot, '
      'never from registry tables.';
  END IF;
END
$$;

GRANT rds_replication TO cdc_reader;
GRANT rds_iam TO cdc_reader;
GRANT CONNECT ON DATABASE biodata_registry TO cdc_reader;

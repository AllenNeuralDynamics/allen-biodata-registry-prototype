-- +runner: no-transaction
-- ============================================================================
-- Migration 0008 — CDC indexer role and logical replication slot
--
-- The runner directive above forces autocommit mode for this file because
-- pg_create_logical_replication_slot() cannot run inside a transaction
-- that has performed writes.
--
-- Sets up the prerequisites for the CDC pipeline (Task 17.1) so the
-- cdc-reader Lambda can poll Aurora's WAL and emit change events to SQS,
-- and the indexing Lambda can connect with BYPASSRLS to hydrate event
-- payloads.
--
-- Idempotency:
--   * CREATE ROLE wrapped in DO block (Postgres has no CREATE ROLE IF NOT EXISTS).
--   * GRANT statements are idempotent (re-grant is a no-op).
--   * Replication slot creation guarded by SELECT-then-create pattern.
--
-- Validates: R28.1, R28.2.
-- Design: §Architecture.CDC Pipeline Architecture, §Components.12. Indexing_Lambda.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. cdc_indexer role
--
-- Used by:
--   * cdc-reader Lambda — reads from `biodata_cdc` slot via
--     pg_logical_slot_get_changes().
--   * Indexing Lambda — JOINs to hydrate denormalized payloads.
--
-- BYPASSRLS is the key privilege: the indexer must see every row
-- regardless of governance scope so it can produce the
-- space_id/org_id/is_sensitive metadata for downstream RLS-aware queries.
--
-- LOGIN + rds_iam: connection happens via Aurora IAM database authentication;
-- no static password.
--
-- REPLICATION: required for pg_logical_slot_get_changes() and the
-- pg_create_logical_replication_slot() call below.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cdc_indexer') THEN
    -- Aurora-specific: only roles with REPLICATION can create REPLICATION
    -- roles, but Aurora reserves REPLICATION for itself. The Aurora-managed
    -- `rds_replication` role provides the equivalent privilege through
    -- group membership, and GRANTing it AFTER the role is created works
    -- without needing the migration_runner role to itself hold REPLICATION.
    --
    -- BYPASSRLS still works (rds_superuser can grant it).
    CREATE ROLE cdc_indexer WITH LOGIN BYPASSRLS;
    COMMENT ON ROLE cdc_indexer IS
      'CDC pipeline service role. BYPASSRLS for cross-tenant visibility-metadata '
      'hydration; rds_replication membership for logical replication slot reads. '
      'Authenticates via Aurora IAM (rds_iam grant below).';
  END IF;
END
$$;

-- Aurora-managed group equivalent of REPLICATION attribute.
GRANT rds_replication TO cdc_indexer;

-- Grant rds_iam so the Lambda can authenticate with IAM auth tokens.
GRANT rds_iam TO cdc_indexer;

-- Grant CONNECT on the database. (The role is created via biodata_admin,
-- so without this grant cdc_indexer cannot open a session.)
GRANT CONNECT ON DATABASE biodata_registry TO cdc_indexer;

-- Grant USAGE on the public schema so the indexer can SELECT from registry tables
-- to hydrate payloads.
GRANT USAGE ON SCHEMA public TO cdc_indexer;

-- Grant SELECT on every existing table — and every future table — in public.
-- The indexer needs to JOIN across the full registry graph to denormalize.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO cdc_indexer;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO cdc_indexer;

-- ---------------------------------------------------------------------------
-- Replication slot + publication moved to 0009_cdc_replication_slot.sql
--
-- pg_create_logical_replication_slot() cannot run in a connection that
-- has performed writes (even with autocommit). Splitting it into a
-- separate migration file forces the runner to open a fresh connection
-- where no writes have happened yet.
-- ---------------------------------------------------------------------------

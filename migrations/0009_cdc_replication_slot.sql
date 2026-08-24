-- +runner: no-transaction
-- ============================================================================
-- Migration 0009 — biodata_cdc logical replication slot + biodata_pub
--
-- Split out from 0008 because pg_create_logical_replication_slot() refuses
-- to run in any transaction that has performed writes — even in autocommit
-- mode, an earlier DDL statement in the same connection is enough to
-- trigger "cannot create logical replication slot in transaction that has
-- performed writes". Putting the slot in its own file forces the runner
-- to open a fresh connection where no writes have happened yet.
--
-- Idempotency:
--   * Slot creation is guarded by SELECT against pg_replication_slots.
--   * Publication creation is guarded by SELECT against pg_publication.
--
-- Validates: R28.1, R28.2.
-- Design: §Architecture.CDC Pipeline Architecture.
-- ============================================================================

-- biodata_cdc logical replication slot. Created with the `pgoutput` plugin
-- (built into Postgres 10+; ships with Aurora PostgreSQL 16). Slot
-- persistence guarantees an unconsumed WAL position is retained across
-- cdc-reader Lambda invocations even when the Lambda's container is
-- recycled.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_replication_slots WHERE slot_name = 'biodata_cdc'
  ) THEN
    PERFORM pg_create_logical_replication_slot('biodata_cdc', 'pgoutput');
  END IF;
END
$$;

-- biodata_pub publication for pgoutput-driven CDC. The cdc-reader filters
-- by table name in application code rather than at the publication level,
-- so adding new tables doesn't require touching the publication on every
-- schema migration.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_publication WHERE pubname = 'biodata_pub'
  ) THEN
    CREATE PUBLICATION biodata_pub FOR ALL TABLES;
  END IF;
END
$$;

-- ============================================================================
-- Migration 0011 — biodata_app role for application Lambdas
--
-- The Authorizer, Registration, Search, Validation, Lifecycle, and other
-- business Lambdas authenticate as `biodata_app` via Aurora IAM database
-- authentication. Unlike the privileged migration_runner / cdc_indexer
-- roles, biodata_app respects RLS — every connection issues
-- `SET LOCAL app.current_user_id/space_ids/roles` to scope visibility.
--
-- Validates: R10.1, R10.2 (Layer 2 — Database RLS).
-- ============================================================================

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'biodata_app') THEN
    CREATE ROLE biodata_app WITH LOGIN;
    COMMENT ON ROLE biodata_app IS
      'Application service role for the business Lambda fleet. Respects RLS '
      '(no BYPASSRLS); every connection sets app.current_user_id/space_ids/'
      'roles via the shared layer connection helper. Authenticates via '
      'Aurora IAM (rds_iam grant below).';
  END IF;
END
$$;

GRANT rds_iam TO biodata_app;
GRANT CONNECT ON DATABASE biodata_registry TO biodata_app;
GRANT USAGE ON SCHEMA public TO biodata_app;

-- Grant CRUD privileges on every existing table — and every future table
-- — in public. The `biodata_app` role is the workhorse for all business
-- writes; production should split this into per-Lambda roles with
-- table-level grants once the schema stabilises.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO biodata_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO biodata_app;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO biodata_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO biodata_app;

-- entity_revision is intentionally INSERT/SELECT only (no UPDATE/DELETE)
-- — migration 0004 issues `REVOKE UPDATE, DELETE ON entity_revision FROM
-- PUBLIC` to enforce revision immutability (R6.1, R26.4). Re-revoke
-- explicitly so an accidental future grant chain doesn't undermine the
-- guarantee.
REVOKE UPDATE, DELETE ON entity_revision FROM biodata_app;

-- Grant the application role membership in biodata_app_role (created in
-- migration 0006) so the role-based RLS policies match.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'biodata_app_role') THEN
    GRANT biodata_app_role TO biodata_app;
  END IF;
END
$$;

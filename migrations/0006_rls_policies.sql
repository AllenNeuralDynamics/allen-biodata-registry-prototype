-- =============================================================================
-- Migration: 0006_rls_policies.sql
-- Purpose:   Enable Row Level Security (RLS) on the registry's user-facing
--            tables and create the per-table policies + redacted views that
--            implement Layer 2 of the three-layer enforcement model
--            (Application -> Database RLS -> API Sensitive_Flag).
--
-- Spec:      .kiro/specs/allen-biodata-registry-poc
-- Task:      7.6 — Create migration 0006_rls_policies.sql
-- Validates: R6.5, R8.1, R10.1, R10.2, R10.3, R13.2
-- Design:    §Data Models.Aurora.RLS policies
--            §Architecture.RLS Enforcement Architecture
--
-- Idempotency:
--   * CREATE ROLE is wrapped in a DO block (Postgres has no
--     CREATE ROLE IF NOT EXISTS).
--   * Helper functions use CREATE OR REPLACE FUNCTION.
--   * Policies use DROP POLICY IF EXISTS ... ; CREATE POLICY ... so re-runs
--     reapply the latest definition without erroring.
--   * Views use CREATE OR REPLACE VIEW.
--   * GRANT / REVOKE are inherently idempotent.
--   * ALTER TABLE ... ENABLE / FORCE ROW LEVEL SECURITY is idempotent.
--   * Re-running this migration after a successful first run is a no-op.
--
-- Ordering:
--   * The migration runner applies *.sql files in lexical order, so
--     0001..0005 have already executed and the referenced tables exist:
--       0001_governance.sql               -> organization, space, app_user,
--                                            user_org_role, user_space_role,
--                                            sharing_grant
--       0002_data_asset.sql               -> data_asset, subject, instrument,
--                                            rig, procedures, session,
--                                            acquisition, processing,
--                                            quality_control, data_description
--       0003_junctions.sql                -> data_asset_subject,
--                                            data_asset_instrument,
--                                            data_asset_rig,
--                                            data_asset_procedures
--       0004_revisions_lifecycle_duplicates.sql -> entity_revision,
--                                            lifecycle_transition,
--                                            duplicate_flag
--       0005_collections_schemas.sql      -> collection, collection_asset,
--                                            collection_hierarchy,
--                                            schema_definition
--
-- =============================================================================
-- Session GUCs the application MUST set on every connection (Layer 2 of the
-- three-layer enforcement model). Migrations DO NOT set these — they are
-- read via current_setting(...,true) so the policies still parse when the
-- GUC is absent (true = missing_ok). Application code uses SET LOCAL inside
-- a transaction so the values clear at COMMIT/ROLLBACK.
--
--   app.current_user_id        — UUID (app_user.id) of the authenticated
--                                user. NULL/missing for system contexts.
--   app.current_user_role_set  — comma-separated list of role_kind values
--                                aggregated from user_org_role,
--                                user_space_role, and sharing_grant
--                                (e.g. 'viewer,data_administrator').
--   app.current_org_ids        — comma-separated list of organization UUIDs
--                                where the user holds an org-level role.
--   app.current_space_ids      — comma-separated list of space UUIDs where
--                                the user holds a space-level role
--                                (directly or via inherited org role or
--                                a sharing_grant).
--
-- All four GUCs are TEXT (Postgres GUCs cannot natively hold arrays). The
-- helper functions below parse them via string_to_array(...,',').
-- =============================================================================
-- Visibility model (high-level, see helper functions and policies below):
--
--   A data_asset row is VISIBLE to the current user iff ANY of:
--     (a) PUBLIC      — lifecycle_state = 'published' AND
--                       validation_status = 'valid'
--     (b) SPACE-LOCAL — data_asset.space_id ∈ app.current_space_ids
--     (c) ORG-LOCAL   — owning space's org_id ∈ app.current_org_ids
--     (d) SHARED      — a sharing_grant references this asset's space or org
--                       and names the current user / user's org / user's
--                       space as grantee
--
--   AND, layered on top, sensitive_flag = false OR caller has the
--   'data_administrator' or 'org_admin' role (data_asset_sensitive_policy,
--   evaluated as a RESTRICTIVE policy AND'd with the read predicate).
--
-- Asset-specific tables (session, acquisition, processing, quality_control,
-- data_description) and junctions (data_asset_subject, ...) re-use the
-- visibility predicate transitively via EXISTS (SELECT 1 FROM data_asset ...).
-- Because data_asset itself has RLS enabled, the inner SELECT is filtered by
-- data_asset_read_policy + data_asset_sensitive_policy automatically — no
-- duplicated predicate logic, no risk of drift between layers.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- Application role: biodata_app_role
-- -----------------------------------------------------------------------------
-- The application connects to Aurora using credentials mapped to this role.
-- Migration runner creates it (NOLOGIN — credentials are issued via
-- Secrets Manager / IAM auth, the role itself just bundles privileges).
--
-- This role is FORCED to obey RLS even when it owns the underlying tables
-- (see ALTER TABLE ... FORCE ROW LEVEL SECURITY below). The cluster admin
-- (rds_superuser / postgres) bypasses RLS by default — the migration runner
-- runs as that admin, so policy creation itself is unaffected.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'biodata_app_role') THEN
    CREATE ROLE biodata_app_role NOLOGIN;
    COMMENT ON ROLE biodata_app_role IS
      'Application role for the biodata-registry Lambda fleet. NOLOGIN — '
      'credentials issued via Secrets Manager / IAM. Subject to RLS via '
      'FORCE ROW LEVEL SECURITY on every user-facing table.';
  END IF;
END;
$$;


-- =============================================================================
-- Helper functions
-- =============================================================================
-- Parse session GUCs once each; policies call these instead of repeating the
-- string_to_array(coalesce(current_setting(...,true),''),',') idiom.
--
-- All helpers are STABLE (deterministic within a query), pure-SQL where
-- possible. Callable by any role (no SECURITY DEFINER needed — they only
-- read GUCs, no privileged catalog access).
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION app_user_id()
  RETURNS uuid
  LANGUAGE sql
  STABLE
AS $$
  SELECT NULLIF(current_setting('app.current_user_id', true), '')::uuid;
$$;

COMMENT ON FUNCTION app_user_id() IS
  'Returns the authenticated user UUID from the app.current_user_id GUC, '
  'or NULL when the GUC is unset (system / migration contexts).';


CREATE OR REPLACE FUNCTION app_role_set()
  RETURNS text[]
  LANGUAGE sql
  STABLE
AS $$
  SELECT string_to_array(
    coalesce(current_setting('app.current_user_role_set', true), ''),
    ','
  );
$$;

COMMENT ON FUNCTION app_role_set() IS
  'Returns the caller''s aggregated role_kind values as a text[] from the '
  'app.current_user_role_set GUC. Empty array when unset.';


CREATE OR REPLACE FUNCTION app_org_ids()
  RETURNS text[]
  LANGUAGE sql
  STABLE
AS $$
  SELECT string_to_array(
    coalesce(current_setting('app.current_org_ids', true), ''),
    ','
  );
$$;

COMMENT ON FUNCTION app_org_ids() IS
  'Returns the organization UUIDs (as text) the caller holds an org-level '
  'role on, parsed from app.current_org_ids. Empty array when unset.';


CREATE OR REPLACE FUNCTION app_space_ids()
  RETURNS text[]
  LANGUAGE sql
  STABLE
AS $$
  SELECT string_to_array(
    coalesce(current_setting('app.current_space_ids', true), ''),
    ','
  );
$$;

COMMENT ON FUNCTION app_space_ids() IS
  'Returns the space UUIDs (as text) the caller has access to (direct '
  'space role + inherited via org role + via sharing_grant), parsed from '
  'app.current_space_ids. Empty array when unset.';


CREATE OR REPLACE FUNCTION is_data_admin()
  RETURNS boolean
  LANGUAGE sql
  STABLE
AS $$
  SELECT 'data_administrator' = ANY(app_role_set());
$$;

COMMENT ON FUNCTION is_data_admin() IS
  'True iff the caller''s role set contains ''data_administrator''. '
  'Used by data_asset_sensitive_policy and subject_viewer_v.';


CREATE OR REPLACE FUNCTION is_org_admin()
  RETURNS boolean
  LANGUAGE sql
  STABLE
AS $$
  SELECT 'org_admin' = ANY(app_role_set());
$$;

COMMENT ON FUNCTION is_org_admin() IS
  'True iff the caller''s role set contains ''org_admin''. '
  'Used by sharing_grant_org_admin_policy.';


-- =============================================================================
-- ENABLE + FORCE ROW LEVEL SECURITY
-- =============================================================================
-- ENABLE turns RLS on. FORCE makes RLS apply even to the table owner — without
-- this, the role that owns the table (typically the migration runner /
-- biodata_app_role-adjacent owner) would silently bypass policies.
--
-- ALTER TABLE ... ENABLE / FORCE ROW LEVEL SECURITY is idempotent.
--
-- Tables explicitly enumerated by the task brief: data_asset, subject,
-- entity_revision, sharing_grant, and "all asset-specific entity tables"
-- (session, acquisition, processing, quality_control, data_description).
-- We additionally enable RLS on the four data_asset_* junction tables
-- created in 0003 because their visibility is unambiguously tied to the
-- referenced data_asset (task brief: "junction tables: same pattern —
-- visible iff data_asset is visible").
--
-- Other shared entities (instrument, rig, procedures) are NOT listed in
-- the task brief and contain no governance scoping (no space_id, no
-- sensitive flag); they are global registry data of low confidentiality.
-- We deliberately leave RLS OFF on those to keep policy surface area
-- minimal — the data_asset visibility predicate already gates visibility
-- of which assets reference them.
-- -----------------------------------------------------------------------------

-- Core
ALTER TABLE data_asset       ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_asset       FORCE  ROW LEVEL SECURITY;

-- Shared entity called out by name in task brief
ALTER TABLE subject          ENABLE ROW LEVEL SECURITY;
ALTER TABLE subject          FORCE  ROW LEVEL SECURITY;

-- Audit trail
ALTER TABLE entity_revision  ENABLE ROW LEVEL SECURITY;
ALTER TABLE entity_revision  FORCE  ROW LEVEL SECURITY;

-- Cross-org sharing
ALTER TABLE sharing_grant    ENABLE ROW LEVEL SECURITY;
ALTER TABLE sharing_grant    FORCE  ROW LEVEL SECURITY;

-- Asset-specific entities (1:N from data_asset, FK with ON DELETE CASCADE)
ALTER TABLE session          ENABLE ROW LEVEL SECURITY;
ALTER TABLE session          FORCE  ROW LEVEL SECURITY;
ALTER TABLE acquisition      ENABLE ROW LEVEL SECURITY;
ALTER TABLE acquisition      FORCE  ROW LEVEL SECURITY;
ALTER TABLE processing       ENABLE ROW LEVEL SECURITY;
ALTER TABLE processing       FORCE  ROW LEVEL SECURITY;
ALTER TABLE quality_control  ENABLE ROW LEVEL SECURITY;
ALTER TABLE quality_control  FORCE  ROW LEVEL SECURITY;
ALTER TABLE data_description ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_description FORCE  ROW LEVEL SECURITY;

-- M:N junction tables (visible iff referenced data_asset is visible)
ALTER TABLE data_asset_subject    ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_asset_subject    FORCE  ROW LEVEL SECURITY;
ALTER TABLE data_asset_instrument ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_asset_instrument FORCE  ROW LEVEL SECURITY;
ALTER TABLE data_asset_rig        ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_asset_rig        FORCE  ROW LEVEL SECURITY;
ALTER TABLE data_asset_procedures ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_asset_procedures FORCE  ROW LEVEL SECURITY;


-- =============================================================================
-- Policies: data_asset
-- =============================================================================
-- Three policies layered together:
--   1. data_asset_read_policy      (PERMISSIVE, FOR SELECT)
--      USING: union of (a) public published-valid, (b) space-local,
--             (c) org-local, (d) shared via sharing_grant.
--   2. data_asset_sensitive_policy (RESTRICTIVE, FOR SELECT)
--      USING: sensitive_flag = false OR caller is data_administrator/org_admin.
--      Restrictive => AND-combined with the permissive read policy.
--   3. data_asset_write_policy     (PERMISSIVE, FOR INSERT, UPDATE, DELETE)
--      Caller must hold 'data_administrator' AND have access to the asset's
--      space (space-local) or owning org (org-local).
-- -----------------------------------------------------------------------------

DROP POLICY IF EXISTS data_asset_read_policy ON data_asset;
CREATE POLICY data_asset_read_policy
  ON data_asset
  AS PERMISSIVE
  FOR SELECT
  TO PUBLIC
  USING (
    -- (a) PUBLIC: published-and-valid assets are visible to everyone,
    -- including unauthenticated callers (no GUC required).
    (lifecycle_state = 'published' AND validation_status = 'valid')

    -- (b) SPACE-LOCAL: caller has any role on the asset's space.
    OR space_id::text = ANY(app_space_ids())

    -- (c) ORG-LOCAL: caller has any role on the org that owns the asset's
    -- space.
    OR EXISTS (
      SELECT 1
      FROM space sp
      WHERE sp.id = data_asset.space_id
        AND sp.org_id::text = ANY(app_org_ids())
    )

    -- (d) SHARED: a sharing_grant from the asset's owning org names the
    -- caller's user / org / space as grantee. Expired grants are excluded.
    -- The sharing_grant table itself has its own RLS (sharing_grant_org_admin_policy)
    -- but that policy is bypassed inside this subquery because the planner
    -- only treats the outer query's role as the RLS subject for nested
    -- subqueries when those nested tables also have FORCE RLS — which
    -- sharing_grant does. To preserve correctness here we filter on the
    -- grant's principal columns directly without relying on sharing_grant's
    -- own visibility — this works because the SELECT subquery sees zero
    -- rows when the caller is not the grantee, regardless of who the
    -- granter org_admin is.
    OR EXISTS (
      SELECT 1
      FROM sharing_grant sg
      JOIN space sp_asset ON sp_asset.id = data_asset.space_id
      WHERE sg.granter_org_id = sp_asset.org_id
        AND (sg.expires_at IS NULL OR sg.expires_at > now())
        AND (
             sg.grantee_space_id = data_asset.space_id
          OR sg.grantee_org_id::text   = ANY(app_org_ids())
          OR sg.principal_org_id::text = ANY(app_org_ids())
          OR sg.principal_user_id      = app_user_id()
        )
    )
  );

COMMENT ON POLICY data_asset_read_policy ON data_asset IS
  'R10.1, R10.2, R13.2: SELECT visible iff (published+valid) OR space-local '
  'OR org-local OR named in a non-expired sharing_grant.';


DROP POLICY IF EXISTS data_asset_sensitive_policy ON data_asset;
CREATE POLICY data_asset_sensitive_policy
  ON data_asset
  AS RESTRICTIVE
  FOR SELECT
  TO PUBLIC
  USING (
    sensitive_flag = false
    OR is_data_admin()
    OR is_org_admin()
  );

COMMENT ON POLICY data_asset_sensitive_policy ON data_asset IS
  'R8.1: RESTRICTIVE — assets with sensitive_flag=true are only visible to '
  'data_administrator or org_admin role-holders. AND-combined with '
  'data_asset_read_policy.';


DROP POLICY IF EXISTS data_asset_write_policy ON data_asset;
CREATE POLICY data_asset_write_policy
  ON data_asset
  AS PERMISSIVE
  FOR ALL
  TO PUBLIC
  USING (
    -- USING: filters which existing rows can be UPDATE / DELETE targets.
    is_data_admin()
    AND (
      space_id::text = ANY(app_space_ids())
      OR EXISTS (
        SELECT 1 FROM space sp
        WHERE sp.id = data_asset.space_id
          AND sp.org_id::text = ANY(app_org_ids())
      )
    )
  )
  WITH CHECK (
    -- WITH CHECK: validates new row state on INSERT / UPDATE.
    is_data_admin()
    AND (
      space_id::text = ANY(app_space_ids())
      OR EXISTS (
        SELECT 1 FROM space sp
        WHERE sp.id = data_asset.space_id
          AND sp.org_id::text = ANY(app_org_ids())
      )
    )
  );

COMMENT ON POLICY data_asset_write_policy ON data_asset IS
  'R10.1, R13.2: INSERT/UPDATE/DELETE require data_administrator role '
  'scoped to the asset''s space or owning org. Combined OR with '
  'data_asset_read_policy on SELECT (no extra read access — admins are '
  'already space/org members so read_policy already covers them).';


-- =============================================================================
-- Policies: subject
-- =============================================================================
-- Subjects are shared records (R25.5) reused across many data_assets via the
-- data_asset_subject junction. A subject is visible if the caller can see at
-- least one data_asset that references it, OR if the caller is a
-- data_administrator (administrative read for governance / dedup workflows).
--
-- The EXISTS subquery against data_asset relies on data_asset's own RLS to
-- filter — no duplicated predicate. If the inner SELECT returns zero rows
-- because data_asset_read_policy / data_asset_sensitive_policy hide them,
-- the outer EXISTS is false and the subject row is hidden.
-- -----------------------------------------------------------------------------

DROP POLICY IF EXISTS subject_rls_policy ON subject;
CREATE POLICY subject_rls_policy
  ON subject
  AS PERMISSIVE
  FOR ALL
  TO PUBLIC
  USING (
    is_data_admin()
    OR EXISTS (
      SELECT 1
      FROM data_asset_subject das
      JOIN data_asset da ON da.id = das.data_asset_id
      WHERE das.subject_id = subject.id
    )
  )
  WITH CHECK (
    -- Writes (INSERT/UPDATE) require data_administrator. Subjects are
    -- governance-significant (date_of_birth, genotype) and shared, so we
    -- don't let space-level roles edit them ad-hoc.
    is_data_admin()
  );

COMMENT ON POLICY subject_rls_policy ON subject IS
  'R10.1, R10.3: subjects are visible iff the caller can see a data_asset '
  'that references them (transitive via data_asset RLS) or is a '
  'data_administrator. Writes require data_administrator.';


-- =============================================================================
-- View: subject_viewer_v (column-level redaction for date_of_birth)
-- =============================================================================
-- R10.3: data_administrator sees the real date_of_birth; everyone else sees
-- NULL. Application code MUST query this view (and not the base subject
-- table) — REVOKE on the base table at the bottom of this migration enforces
-- that mechanically for biodata_app_role.
--
-- The CASE expression replicates the helper function inline so the view
-- definition is self-contained and pglast / EXPLAIN can show the redaction
-- predicate without a function lookup.
-- -----------------------------------------------------------------------------

CREATE OR REPLACE VIEW subject_viewer_v AS
SELECT
  s.id,
  s.subject_id,
  s.species,
  s.sex,
  CASE
    WHEN 'data_administrator' = ANY(
           string_to_array(
             coalesce(current_setting('app.current_user_role_set', true), ''),
             ','
           )
         )
    THEN s.date_of_birth
    ELSE NULL
  END AS date_of_birth,
  s.genotype,
  s.source,
  s.weight_at_acquisition_g,
  s.age_at_acquisition_days,
  s.notes,
  s.metadata,
  s.embedding,
  s.description_vec_status,
  s.created_by,
  s.created_at
FROM subject s;

COMMENT ON VIEW subject_viewer_v IS
  'R10.3: column-level redaction over subject. date_of_birth is NULL unless '
  'the caller''s role set contains ''data_administrator''. Application code '
  'must SELECT from this view; the base subject table is REVOKEd from '
  'biodata_app_role.';


-- =============================================================================
-- Policies: entity_revision
-- =============================================================================
-- R6.5: a revision row is visible only if the caller can see the underlying
-- entity. Because entity_revision.entity_type is polymorphic, the policy
-- branches on the type and EXISTS-checks the appropriate base table. Each
-- base table has its own RLS, so the inner SELECT is filtered automatically
-- — no duplicated predicate, no risk of drift.
--
-- For entity types not covered explicitly (e.g. instrument, rig — not RLS-
-- enabled) the predicate falls through and the row is visible only to
-- data_administrator. This is conservative: revision history of unsecured
-- entities still requires admin read.
--
-- entity_revision is INSERT-only by design (R6.1: REVOKE UPDATE, DELETE).
-- The policy below covers SELECT (USING) and INSERT (WITH CHECK).
-- -----------------------------------------------------------------------------

DROP POLICY IF EXISTS entity_revision_rls_policy ON entity_revision;
CREATE POLICY entity_revision_rls_policy
  ON entity_revision
  AS PERMISSIVE
  FOR ALL
  TO PUBLIC
  USING (
    is_data_admin()
    OR (
      entity_type = 'data_asset' AND EXISTS (
        SELECT 1 FROM data_asset da WHERE da.id = entity_revision.entity_id
      )
    )
    OR (
      entity_type = 'subject' AND EXISTS (
        SELECT 1 FROM subject s WHERE s.id = entity_revision.entity_id
      )
    )
    OR (
      entity_type = 'session' AND EXISTS (
        SELECT 1 FROM session se WHERE se.id = entity_revision.entity_id
      )
    )
    OR (
      entity_type = 'acquisition' AND EXISTS (
        SELECT 1 FROM acquisition ac WHERE ac.id = entity_revision.entity_id
      )
    )
    OR (
      entity_type = 'processing' AND EXISTS (
        SELECT 1 FROM processing pr WHERE pr.id = entity_revision.entity_id
      )
    )
    OR (
      entity_type = 'quality_control' AND EXISTS (
        SELECT 1 FROM quality_control qc WHERE qc.id = entity_revision.entity_id
      )
    )
    OR (
      entity_type = 'data_description' AND EXISTS (
        SELECT 1 FROM data_description dd WHERE dd.id = entity_revision.entity_id
      )
    )
  )
  WITH CHECK (
    -- INSERT must be made by an authenticated user, and the user_id on the
    -- revision row must match the GUC (writers cannot forge audit entries
    -- as another user).
    user_id = app_user_id()
  );

COMMENT ON POLICY entity_revision_rls_policy ON entity_revision IS
  'R6.5, R10.1: revisions are visible iff the caller can see the underlying '
  'entity (resolved via EXISTS against the base table, which has its own '
  'RLS). INSERT requires user_id = app_user_id() to prevent forged audit '
  'entries. UPDATE/DELETE are blocked at the GRANT level by R6.1.';


-- =============================================================================
-- Policies: sharing_grant
-- =============================================================================
-- R9.5, R9.6: only org_admin role-holders see and manage sharing_grants for
-- the orgs they administer. Non-admins never need to enumerate grants — the
-- effect of grants flows through their app.current_space_ids GUC and is
-- already reflected in data_asset_read_policy.
--
-- The policy permits SELECT/INSERT/UPDATE/DELETE only when the caller holds
-- 'org_admin' AND has org-level access to either side of the grant
-- (granter_org_id or grantee_org_id / principal_org_id).
-- -----------------------------------------------------------------------------

DROP POLICY IF EXISTS sharing_grant_org_admin_policy ON sharing_grant;
CREATE POLICY sharing_grant_org_admin_policy
  ON sharing_grant
  AS PERMISSIVE
  FOR ALL
  TO PUBLIC
  USING (
    is_org_admin()
    AND (
         granter_org_id::text   = ANY(app_org_ids())
      OR grantee_org_id::text   = ANY(app_org_ids())
      OR principal_org_id::text = ANY(app_org_ids())
    )
  )
  WITH CHECK (
    is_org_admin()
    AND (
         granter_org_id::text   = ANY(app_org_ids())
      OR grantee_org_id::text   = ANY(app_org_ids())
      OR principal_org_id::text = ANY(app_org_ids())
    )
  );

COMMENT ON POLICY sharing_grant_org_admin_policy ON sharing_grant IS
  'R9.5, R9.6: only org_admin holders whose app.current_org_ids includes '
  'either the granter or grantee org can SELECT/INSERT/UPDATE/DELETE the '
  'grant. Non-admin users never need to enumerate grants directly.';


-- =============================================================================
-- Policies: asset-specific tables
-- =============================================================================
-- session, acquisition, processing, quality_control, data_description each
-- carry data_asset_id with ON DELETE CASCADE (see 0002). Visibility is
-- transitively inherited from the data_asset via EXISTS — relying on
-- data_asset's RLS for the predicate so there is exactly one source of
-- truth for "can the caller see asset X?".
-- -----------------------------------------------------------------------------

DROP POLICY IF EXISTS session_rls_policy ON session;
CREATE POLICY session_rls_policy
  ON session
  AS PERMISSIVE
  FOR ALL
  TO PUBLIC
  USING (
    EXISTS (SELECT 1 FROM data_asset da WHERE da.id = session.data_asset_id)
  )
  WITH CHECK (
    EXISTS (SELECT 1 FROM data_asset da WHERE da.id = session.data_asset_id)
  );

COMMENT ON POLICY session_rls_policy ON session IS
  'R10.1: session visible iff its data_asset is visible (transitive via '
  'data_asset RLS).';


DROP POLICY IF EXISTS acquisition_rls_policy ON acquisition;
CREATE POLICY acquisition_rls_policy
  ON acquisition
  AS PERMISSIVE
  FOR ALL
  TO PUBLIC
  USING (
    EXISTS (SELECT 1 FROM data_asset da WHERE da.id = acquisition.data_asset_id)
  )
  WITH CHECK (
    EXISTS (SELECT 1 FROM data_asset da WHERE da.id = acquisition.data_asset_id)
  );

COMMENT ON POLICY acquisition_rls_policy ON acquisition IS
  'R10.1: acquisition visible iff its data_asset is visible (transitive '
  'via data_asset RLS).';


DROP POLICY IF EXISTS processing_rls_policy ON processing;
CREATE POLICY processing_rls_policy
  ON processing
  AS PERMISSIVE
  FOR ALL
  TO PUBLIC
  USING (
    EXISTS (SELECT 1 FROM data_asset da WHERE da.id = processing.data_asset_id)
  )
  WITH CHECK (
    EXISTS (SELECT 1 FROM data_asset da WHERE da.id = processing.data_asset_id)
  );

COMMENT ON POLICY processing_rls_policy ON processing IS
  'R10.1: processing visible iff its data_asset is visible (transitive '
  'via data_asset RLS).';


DROP POLICY IF EXISTS quality_control_rls_policy ON quality_control;
CREATE POLICY quality_control_rls_policy
  ON quality_control
  AS PERMISSIVE
  FOR ALL
  TO PUBLIC
  USING (
    EXISTS (SELECT 1 FROM data_asset da WHERE da.id = quality_control.data_asset_id)
  )
  WITH CHECK (
    EXISTS (SELECT 1 FROM data_asset da WHERE da.id = quality_control.data_asset_id)
  );

COMMENT ON POLICY quality_control_rls_policy ON quality_control IS
  'R10.1: quality_control visible iff its data_asset is visible (transitive '
  'via data_asset RLS).';


DROP POLICY IF EXISTS data_description_rls_policy ON data_description;
CREATE POLICY data_description_rls_policy
  ON data_description
  AS PERMISSIVE
  FOR ALL
  TO PUBLIC
  USING (
    EXISTS (SELECT 1 FROM data_asset da WHERE da.id = data_description.data_asset_id)
  )
  WITH CHECK (
    EXISTS (SELECT 1 FROM data_asset da WHERE da.id = data_description.data_asset_id)
  );

COMMENT ON POLICY data_description_rls_policy ON data_description IS
  'R10.1: data_description visible iff its data_asset is visible '
  '(transitive via data_asset RLS).';


-- =============================================================================
-- Policies: junction tables
-- =============================================================================
-- data_asset_subject, data_asset_instrument, data_asset_rig,
-- data_asset_procedures: visible iff the linked data_asset is visible
-- (task brief: "junction tables: same pattern — visible iff data_asset is
-- visible").
-- -----------------------------------------------------------------------------

DROP POLICY IF EXISTS data_asset_subject_rls_policy ON data_asset_subject;
CREATE POLICY data_asset_subject_rls_policy
  ON data_asset_subject
  AS PERMISSIVE
  FOR ALL
  TO PUBLIC
  USING (
    EXISTS (SELECT 1 FROM data_asset da WHERE da.id = data_asset_subject.data_asset_id)
  )
  WITH CHECK (
    EXISTS (SELECT 1 FROM data_asset da WHERE da.id = data_asset_subject.data_asset_id)
  );

COMMENT ON POLICY data_asset_subject_rls_policy ON data_asset_subject IS
  'R10.1: junction visible iff linked data_asset is visible.';


DROP POLICY IF EXISTS data_asset_instrument_rls_policy ON data_asset_instrument;
CREATE POLICY data_asset_instrument_rls_policy
  ON data_asset_instrument
  AS PERMISSIVE
  FOR ALL
  TO PUBLIC
  USING (
    EXISTS (SELECT 1 FROM data_asset da WHERE da.id = data_asset_instrument.data_asset_id)
  )
  WITH CHECK (
    EXISTS (SELECT 1 FROM data_asset da WHERE da.id = data_asset_instrument.data_asset_id)
  );

COMMENT ON POLICY data_asset_instrument_rls_policy ON data_asset_instrument IS
  'R10.1: junction visible iff linked data_asset is visible.';


DROP POLICY IF EXISTS data_asset_rig_rls_policy ON data_asset_rig;
CREATE POLICY data_asset_rig_rls_policy
  ON data_asset_rig
  AS PERMISSIVE
  FOR ALL
  TO PUBLIC
  USING (
    EXISTS (SELECT 1 FROM data_asset da WHERE da.id = data_asset_rig.data_asset_id)
  )
  WITH CHECK (
    EXISTS (SELECT 1 FROM data_asset da WHERE da.id = data_asset_rig.data_asset_id)
  );

COMMENT ON POLICY data_asset_rig_rls_policy ON data_asset_rig IS
  'R10.1: junction visible iff linked data_asset is visible.';


DROP POLICY IF EXISTS data_asset_procedures_rls_policy ON data_asset_procedures;
CREATE POLICY data_asset_procedures_rls_policy
  ON data_asset_procedures
  AS PERMISSIVE
  FOR ALL
  TO PUBLIC
  USING (
    EXISTS (SELECT 1 FROM data_asset da WHERE da.id = data_asset_procedures.data_asset_id)
  )
  WITH CHECK (
    EXISTS (SELECT 1 FROM data_asset da WHERE da.id = data_asset_procedures.data_asset_id)
  );

COMMENT ON POLICY data_asset_procedures_rls_policy ON data_asset_procedures IS
  'R10.1: junction visible iff linked data_asset is visible.';


-- =============================================================================
-- GRANTs / REVOKEs for biodata_app_role
-- =============================================================================
-- The app role gets normal CRUD on tables that have RLS — RLS is the
-- enforcement mechanism, GRANT is just the on/off switch.
--
-- Special cases:
--   * subject — REVOKE direct SELECT so application code is forced to use
--     subject_viewer_v (R10.3). DML on subject is still allowed (writes are
--     gated by subject_rls_policy WITH CHECK = is_data_admin()).
--   * subject_viewer_v — GRANT SELECT so the app can read redacted rows.
--   * entity_revision — REVOKE UPDATE, DELETE per R6.1 immutability
--     (the design.md sketch states this requirement; the GRANT here makes
--     the requirement enforceable).
-- -----------------------------------------------------------------------------

-- Tables: SELECT, INSERT, UPDATE, DELETE for app role (RLS gates access).
GRANT SELECT, INSERT, UPDATE, DELETE ON
  data_asset,
  session,
  acquisition,
  processing,
  quality_control,
  data_description,
  data_asset_subject,
  data_asset_instrument,
  data_asset_rig,
  data_asset_procedures,
  sharing_grant
TO biodata_app_role;

-- Subject base table: the app role gets INSERT/UPDATE/DELETE only.
-- SELECT is revoked so all reads must go through subject_viewer_v.
GRANT INSERT, UPDATE, DELETE ON subject TO biodata_app_role;
REVOKE SELECT ON subject FROM biodata_app_role;

-- Redacted view: SELECT only.
GRANT SELECT ON subject_viewer_v TO biodata_app_role;

-- entity_revision: SELECT + INSERT only (R6.1 immutability).
GRANT SELECT, INSERT ON entity_revision TO biodata_app_role;
REVOKE UPDATE, DELETE ON entity_revision FROM biodata_app_role;
-- BIGSERIAL backing sequence usage so INSERTs that rely on the default work.
GRANT USAGE, SELECT ON SEQUENCE entity_revision_id_seq TO biodata_app_role;

-- Helper functions: EXECUTE granted to PUBLIC (no privilege escalation —
-- they only read GUCs that the caller's session already holds).
GRANT EXECUTE ON FUNCTION
  app_user_id(),
  app_role_set(),
  app_org_ids(),
  app_space_ids(),
  is_data_admin(),
  is_org_admin()
TO PUBLIC;


-- =============================================================================
-- End of migration 0006_rls_policies.sql
-- =============================================================================

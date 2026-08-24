-- =============================================================================
-- Migration: 0001_governance.sql
-- Purpose:   Bootstrap governance schema for the Allen BioData Registry PoC.
--            Creates the organization / space / user / role tables and the
--            sharing_grant table that drive RBAC and cross-org sharing.
--
-- Spec:      .kiro/specs/allen-biodata-registry-poc
-- Task:      7.1 — Create migration 0001_governance.sql
-- Validates: R9.1, R9.2, R9.4, R9.5, R9.7
-- Design:    §Data Models.Aurora.Governance tables
--
-- Idempotency:
--   * Tables, indexes, and extensions use IF NOT EXISTS.
--   * The role_kind enum is wrapped in a DO block guarded by
--     `EXCEPTION WHEN duplicate_object` so re-runs do not error.
--   * Re-running this migration after a successful first run is a no-op.
--
-- Notes:
--   * Authoritative DDL source is design.md §Data Models.Aurora.Governance
--     tables. Where the task brief diverges from design.md (notably the
--     role_kind enum values), this migration follows design.md and
--     requirements R9.2, which canonically defines the four roles as
--     {'org_admin','space_admin','data_administrator','viewer'}.
--   * Additive fields requested by the task brief (display_name,
--     parent_space_id, granted_at, granted_by, expires_at, citext for
--     case-insensitive uniqueness) are layered in on top of design.md
--     without breaking the columns referenced by downstream migrations
--     (e.g. 0006_rls_policies.sql).
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Required extensions
-- -----------------------------------------------------------------------------

-- citext: case-insensitive text for org names, space names, user emails.
-- Two organizations differing only in case ("Allen Institute" vs
-- "allen institute") are the same governance boundary.
CREATE EXTENSION IF NOT EXISTS citext;

-- pgcrypto: provides gen_random_uuid() used as the default for every PK.
CREATE EXTENSION IF NOT EXISTS pgcrypto;


-- -----------------------------------------------------------------------------
-- Enum: role_kind
-- -----------------------------------------------------------------------------
-- Four RBAC roles scoped to Organization and Space (R9.2). The same enum is
-- reused by user_org_role, user_space_role, and sharing_grant so a single
-- vocabulary governs all role grants.
DO $$
BEGIN
  CREATE TYPE role_kind AS ENUM (
    'org_admin',
    'space_admin',
    'data_administrator',
    'viewer'
  );
EXCEPTION
  WHEN duplicate_object THEN
    -- already created on a prior run; nothing to do
    NULL;
END;
$$;

COMMENT ON TYPE role_kind IS
  'RBAC role values for user_org_role, user_space_role, and sharing_grant. Defined by R9.2.';


-- -----------------------------------------------------------------------------
-- Table: organization
-- -----------------------------------------------------------------------------
-- Top-level governance boundary (Allen Institute, University of Washington,
-- etc.). Multiple top-level orgs coexist with no single über-org (R9.4).
CREATE TABLE IF NOT EXISTS organization (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name         CITEXT NOT NULL UNIQUE,
  display_name TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE  organization              IS 'Top-level governance entity. R9.1, R9.4.';
COMMENT ON COLUMN organization.id           IS 'UUID primary key.';
COMMENT ON COLUMN organization.name         IS 'Case-insensitive unique organization slug (e.g. "allen-institute").';
COMMENT ON COLUMN organization.display_name IS 'Human-friendly display name shown in the UI.';
COMMENT ON COLUMN organization.created_at   IS 'Record creation timestamp (UTC).';


-- -----------------------------------------------------------------------------
-- Table: space
-- -----------------------------------------------------------------------------
-- Project-level grouping inside an Organization. Spaces may nest via
-- parent_space_id to model team/sub-team hierarchies. Deletion of an org
-- cascades to its spaces (test fixtures rely on this); FK to parent space is
-- ON DELETE SET NULL so collapsing a sub-tree does not orphan grandchildren.
CREATE TABLE IF NOT EXISTS space (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id          UUID NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  name            CITEXT NOT NULL,
  display_name    TEXT,
  parent_space_id UUID REFERENCES space(id) ON DELETE SET NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (org_id, name),
  CONSTRAINT space_no_self_parent CHECK (parent_space_id IS NULL OR parent_space_id <> id)
);

CREATE INDEX IF NOT EXISTS space_org_id_idx          ON space (org_id);
CREATE INDEX IF NOT EXISTS space_parent_space_id_idx ON space (parent_space_id);

COMMENT ON TABLE  space                 IS 'Project-level grouping within an Organization. R9.1.';
COMMENT ON COLUMN space.id              IS 'UUID primary key.';
COMMENT ON COLUMN space.org_id          IS 'Owning Organization. ON DELETE CASCADE.';
COMMENT ON COLUMN space.name            IS 'Case-insensitive unique-within-org space slug.';
COMMENT ON COLUMN space.display_name    IS 'Human-friendly display name shown in the UI.';
COMMENT ON COLUMN space.parent_space_id IS 'Optional parent space for nesting (self-FK).';
COMMENT ON COLUMN space.created_at      IS 'Record creation timestamp (UTC).';


-- -----------------------------------------------------------------------------
-- Table: app_user
-- -----------------------------------------------------------------------------
-- Aurora-side projection of a Cognito user. Created by the Cognito
-- Post-Confirmation Lambda (Task 5.2) with no role assignments — roles are
-- granted later via Governance_Lambda. org_id is a soft "home org" claim
-- (custom:org_id) used to seed the access-request flow; it does NOT confer
-- any access by itself — visibility is driven by user_org_role,
-- user_space_role, and sharing_grant.
CREATE TABLE IF NOT EXISTS app_user (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  cognito_sub TEXT NOT NULL UNIQUE,
  email       CITEXT NOT NULL UNIQUE,
  org_id      UUID REFERENCES organization(id) ON DELETE SET NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS app_user_org_id_idx ON app_user (org_id);

COMMENT ON TABLE  app_user             IS 'Aurora projection of a Cognito user. R9.7, R19.3.';
COMMENT ON COLUMN app_user.id          IS 'UUID primary key.';
COMMENT ON COLUMN app_user.cognito_sub IS 'Cognito subject claim (sub). Unique.';
COMMENT ON COLUMN app_user.email       IS 'Verified email from Cognito. Case-insensitive unique.';
COMMENT ON COLUMN app_user.org_id      IS 'Soft "home org" claim (custom:org_id). Does not confer access.';
COMMENT ON COLUMN app_user.created_at  IS 'Record creation timestamp (UTC).';


-- -----------------------------------------------------------------------------
-- Table: user_org_role
-- -----------------------------------------------------------------------------
-- Org-level role grants. PK is (user_id, org_id, role) so a user can hold
-- multiple roles on the same org — the Authorizer_Lambda aggregates them
-- into a role set on every request (R9.7).
--
-- ON DELETE behaviour deviates from the design.md sketch: cascading on the
-- user side cleans up grants when an account is deleted (privacy/right-to-be-
-- forgotten), while RESTRICT on the org side prevents accidentally orphaning
-- grants if an org row is deleted before its grants are revoked.
CREATE TABLE IF NOT EXISTS user_org_role (
  user_id    UUID        NOT NULL REFERENCES app_user(id)     ON DELETE CASCADE,
  org_id     UUID        NOT NULL REFERENCES organization(id) ON DELETE RESTRICT,
  role       role_kind   NOT NULL,
  granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  granted_by UUID REFERENCES app_user(id) ON DELETE SET NULL,
  PRIMARY KEY (user_id, org_id, role)
);

CREATE INDEX IF NOT EXISTS user_org_role_org_id_idx ON user_org_role (org_id);

COMMENT ON TABLE  user_org_role            IS 'Org-level role grants. R9.2, R9.7.';
COMMENT ON COLUMN user_org_role.user_id    IS 'Grantee app_user.';
COMMENT ON COLUMN user_org_role.org_id     IS 'Organization the role is scoped to.';
COMMENT ON COLUMN user_org_role.role       IS 'Role granted (see role_kind).';
COMMENT ON COLUMN user_org_role.granted_at IS 'When the role was granted (UTC).';
COMMENT ON COLUMN user_org_role.granted_by IS 'app_user that granted the role (NULL if grantor account is deleted).';


-- -----------------------------------------------------------------------------
-- Table: user_space_role
-- -----------------------------------------------------------------------------
-- Space-level role grants. Same PK / cascade semantics as user_org_role,
-- scoped to a space instead of an org.
CREATE TABLE IF NOT EXISTS user_space_role (
  user_id    UUID        NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  space_id   UUID        NOT NULL REFERENCES space(id)    ON DELETE RESTRICT,
  role       role_kind   NOT NULL,
  granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  granted_by UUID REFERENCES app_user(id) ON DELETE SET NULL,
  PRIMARY KEY (user_id, space_id, role)
);

CREATE INDEX IF NOT EXISTS user_space_role_space_id_idx ON user_space_role (space_id);

COMMENT ON TABLE  user_space_role            IS 'Space-level role grants. R9.2, R9.7.';
COMMENT ON COLUMN user_space_role.user_id    IS 'Grantee app_user.';
COMMENT ON COLUMN user_space_role.space_id   IS 'Space the role is scoped to.';
COMMENT ON COLUMN user_space_role.role       IS 'Role granted (see role_kind).';
COMMENT ON COLUMN user_space_role.granted_at IS 'When the role was granted (UTC).';
COMMENT ON COLUMN user_space_role.granted_by IS 'app_user that granted the role (NULL if grantor account is deleted).';


-- -----------------------------------------------------------------------------
-- Table: sharing_grant
-- -----------------------------------------------------------------------------
-- Cross-org / cross-space sharing. Carries both the design.md grantee shape
-- (grantee_org_id / grantee_space_id) and the broader principal shape from
-- the task brief (principal_user_id / principal_org_id). A single grant may
-- target exactly one principal type — enforced by the
-- sharing_grant_one_principal CHECK.
--
-- The role column lets the granter scope what the grantee can do with the
-- shared resource (e.g. viewer-only). expires_at lets time-bounded shares
-- be modeled directly in the schema; NULL means perpetual.
CREATE TABLE IF NOT EXISTS sharing_grant (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  granter_org_id     UUID NOT NULL REFERENCES organization(id) ON DELETE CASCADE,

  -- Grantee shape per design.md: grant to an org or to a specific space.
  grantee_org_id     UUID REFERENCES organization(id) ON DELETE CASCADE,
  grantee_space_id   UUID REFERENCES space(id)        ON DELETE CASCADE,

  -- Principal shape per task brief: grant to an individual user or an org.
  principal_user_id  UUID REFERENCES app_user(id)     ON DELETE CASCADE,
  principal_org_id   UUID REFERENCES organization(id) ON DELETE CASCADE,

  role               role_kind NOT NULL DEFAULT 'viewer',
  granted_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  granted_by         UUID NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
  expires_at         TIMESTAMPTZ,

  -- Exactly one of the four target columns must be non-null. This keeps the
  -- semantics unambiguous: a sharing_grant always points at one principal.
  CONSTRAINT sharing_grant_one_principal CHECK (
    (
      (grantee_org_id    IS NOT NULL)::int +
      (grantee_space_id  IS NOT NULL)::int +
      (principal_user_id IS NOT NULL)::int +
      (principal_org_id  IS NOT NULL)::int
    ) = 1
  ),

  -- expires_at, when set, must be in the future relative to granted_at.
  CONSTRAINT sharing_grant_expires_after_granted CHECK (
    expires_at IS NULL OR expires_at > granted_at
  )
);

CREATE INDEX IF NOT EXISTS sharing_grant_granter_org_id_idx    ON sharing_grant (granter_org_id);
CREATE INDEX IF NOT EXISTS sharing_grant_grantee_org_id_idx    ON sharing_grant (grantee_org_id)   WHERE grantee_org_id   IS NOT NULL;
CREATE INDEX IF NOT EXISTS sharing_grant_grantee_space_id_idx  ON sharing_grant (grantee_space_id) WHERE grantee_space_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS sharing_grant_principal_user_id_idx ON sharing_grant (principal_user_id) WHERE principal_user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS sharing_grant_principal_org_id_idx  ON sharing_grant (principal_org_id)  WHERE principal_org_id  IS NOT NULL;
CREATE INDEX IF NOT EXISTS sharing_grant_expires_at_idx        ON sharing_grant (expires_at)        WHERE expires_at        IS NOT NULL;

COMMENT ON TABLE  sharing_grant                   IS 'Cross-org/cross-space sharing grants. R9.5, R9.6.';
COMMENT ON COLUMN sharing_grant.id                IS 'UUID primary key.';
COMMENT ON COLUMN sharing_grant.granter_org_id    IS 'Organization extending the share.';
COMMENT ON COLUMN sharing_grant.grantee_org_id    IS 'Recipient org (mutually exclusive with other principal columns).';
COMMENT ON COLUMN sharing_grant.grantee_space_id  IS 'Recipient space (mutually exclusive with other principal columns).';
COMMENT ON COLUMN sharing_grant.principal_user_id IS 'Recipient user (mutually exclusive with other principal columns).';
COMMENT ON COLUMN sharing_grant.principal_org_id  IS 'Recipient org via principal shape (mutually exclusive with other principal columns).';
COMMENT ON COLUMN sharing_grant.role              IS 'Role granted on the shared resource (defaults to viewer).';
COMMENT ON COLUMN sharing_grant.granted_at        IS 'When the grant was created (UTC).';
COMMENT ON COLUMN sharing_grant.granted_by        IS 'app_user that created the grant.';
COMMENT ON COLUMN sharing_grant.expires_at        IS 'Optional expiry; NULL means perpetual.';

# Authorizer Lambda — Allen BioData Registry PoC

API Gateway custom authorizer (REQUEST type) that fronts every
authenticated endpoint. On each invocation it:

1. Validates the Cognito JWT (signature, expiration, audience, issuer,
   `token_use=id`).
2. Resolves `app_user.id`, the role set, the org set, and the space
   set from Aurora (`app_user`, `user_org_role`, `user_space_role`,
   `sharing_grant`).
3. Returns the API Gateway IAM policy with the auth context in the
   `context` field — every downstream business Lambda parses this via
   `biodata_registry_shared.parse_auth_context` and uses the tuple to
   seed Postgres RLS GUCs.

**Validates:** R9.7, R14.4, R19.4, R19.5.

**Design references:**
- `design.md` §Components.1. Authorizer_Lambda.
- `design.md` §Architecture.RLS Enforcement Architecture (this Lambda
  is "Layer 0" — it produces the inputs RLS Layer 2 consumes).

---

## Behavior

### 1. JWT validation

1. Read the bearer token from `event["headers"]["Authorization"]`
   (`Bearer <token>`). Falls back to `event["authorizationToken"]` for
   legacy TOKEN-type wiring.
2. Fetch the JWKS from
   `https://cognito-idp.{region}.amazonaws.com/{user_pool_id}/.well-known/jwks.json`
   via `PyJWKClient`. The JWKS client is **cached at module level for
   1 hour** so warm Lambda containers amortize the fetch cost across
   invocations.
3. Verify signature (`RS256`), expiration, audience (against
   `COGNITO_APP_CLIENT_ID`), issuer (against the constructed Cognito
   issuer URL), and `token_use=id`. Access tokens are rejected because
   they don't carry `email`, which the registry needs for audit logs.

Any validation failure raises `Unauthorized`, which API Gateway maps
to HTTP 401.

### 2. Aurora lookup

Connect to Aurora using IAM database authentication
(`boto3.rds.generate_db_auth_token`). Run four queries:

1. `SELECT id, email FROM app_user WHERE cognito_sub = $1` — resolve
   the registry-side user id. **No row → 401.** The Post-Confirmation
   Lambda creates this row when the user finishes signup (R19.3); a
   missing row indicates a bug worth surfacing.
2. `SELECT org_id, role FROM user_org_role WHERE user_id = $1` —
   org-level roles.
3. `SELECT space_id, role FROM user_space_role WHERE user_id = $1` —
   space-level roles.
4. `SELECT id FROM space WHERE org_id = ANY($1)` — every space
   inside any org the user holds an org-level role on (org-level
   roles inherit access to every space inside the org, matching the
   RLS policy in `0006_rls_policies.sql`).
5. `SELECT DISTINCT s.id FROM sharing_grant sg JOIN space s ON
   s.org_id = sg.granter_org_id WHERE (sg.expires_at IS NULL OR
   sg.expires_at > now()) AND ...` — every space made visible to the
   user via an active sharing grant. The user is a sharing-grant
   principal when any of `principal_user_id`, `principal_org_id`
   (matched against the user's orgs), `grantee_space_id` (matched
   against the user's direct space roles), or `grantee_org_id`
   (matched against the user's orgs) point at them.

The aggregated `{user_id, email, roles, org_ids, space_ids}` is
returned in the API Gateway authorizer policy's `context` field.
**Lists are comma-joined** because the API Gateway authorizer
protocol flattens `context` to scalar strings;
`biodata_registry_shared.parse_auth_context` knows to split them.

### 3. Caching

API Gateway's built-in REQUEST authorizer cache is configured via
`authorizer_result_ttl_in_seconds = 300` in the apigateway Terraform
module (Task 14.1). A successful Allow policy is reused for up to 5
minutes for the same Authorization header value — Aurora is **not**
hit on every request. When a role/sharing-grant changes,
`Governance_Lambda` (Task 26.1) busts the related Redis
`Access_Filter_Cache` entry; the 5-minute API Gateway TTL is the
documented eventual-consistency window in `design.md`
§Architecture.Cache Coherence.

---

## Environment variables

Injected by the `lambdas/authorizer` Terraform module.

| Variable | Required | Purpose |
|---|---|---|
| `COGNITO_USER_POOL_ID` | yes | Used to construct the JWT issuer + JWKS URL. |
| `COGNITO_APP_CLIENT_ID` | yes | Used as the JWT audience. |
| `DB_HOST` | yes | Aurora writer endpoint. |
| `DB_PORT` | no (default `5432`) | Aurora port. |
| `DB_NAME` | yes | Database name. |
| `DB_USER` | yes | DB user with `rds_iam` membership and SELECT on `app_user`, `user_org_role`, `user_space_role`, `sharing_grant`, `space`. |
| `DB_SSLMODE` | no (default `require`) | psycopg SSL mode. |
| `DB_CONNECT_TIMEOUT_SECONDS` | no (default `5`) | TCP/TLS handshake timeout. |
| `AWS_REGION` | provided by Lambda runtime | Used for IAM token + Cognito issuer URL. |
| `LOG_LEVEL` | no (default `INFO`) | Standard Python logging level. |

---

## IAM scoping

The Lambda's execution role grants `rds-db:connect` to a single
`{aurora_cluster_resource_id, db_user}` tuple — no Secrets Manager
access, no other clusters. The DB user must have `rds_iam` membership
plus `SELECT` on the five tables listed above.

---

## Local development

```bash
cd customers/NPO/RSC/Allen_Institute/biodata-registry/services/authorizer-lambda
python -m pip install -e .[test]
python -m pytest -q
```

The unit tests use `unittest.mock` to stub:

- `PyJWKClient.get_signing_key_from_jwt` — returns a known RSA key.
- `boto3.client("rds")` — `generate_db_auth_token` returns a fixed
  string.
- `psycopg.connect` — returns a mock connection whose cursor records
  the executed SQL and returns canned rows.

The tests cover:

1. Valid JWT + valid `app_user` row → returns Allow policy with
   correct context.
2. Invalid JWT signature → raises `Unauthorized`.
3. Expired JWT → raises `Unauthorized`.
4. Wrong audience → raises `Unauthorized`.
5. `token_use=access` (not `id`) → raises `Unauthorized`.
6. Missing `app_user` row → raises `Unauthorized`.
7. Multiple roles (org_admin + space_admin from different rows) →
   roles aggregated correctly.
8. Active `sharing_grant` for the user → grantee_space_id added to
   `space_ids`.
9. Expired `sharing_grant` → NOT added to `space_ids`.
10. Missing Authorization header → raises `Unauthorized`.
11. Bearer prefix stripping (case-insensitive).
12. Property-based test: any well-formed Cognito sub + Aurora row
    produces a syntactically valid policy with the same fields the
    handler was given.

---

## Packaging

Terraform packages this directory plus the runtime deps from
`requirements.txt` into a deployment zip via the
`lambdas/authorizer` module. `psycopg[binary]` ships a precompiled
libpq so no per-platform wheel juggling is required.

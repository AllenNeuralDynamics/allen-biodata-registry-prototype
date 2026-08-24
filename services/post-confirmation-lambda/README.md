# Post-Confirmation Lambda — Allen BioData Registry PoC

Cognito invokes this Lambda once a user finishes confirming their account
(verification code on self-signup, or SAML completion when federation is
enabled later). The Lambda inserts a bare `app_user` row into Aurora
PostgreSQL with the user's Cognito `sub` and `email`, and **no role
assignments** — the user can authenticate immediately but sees only
`lifecycle_state = 'published'` data via Aurora RLS until an org admin
grants them a role through the Governance Lambda's access-request flow.

**Validates:** R19.3 (Post-Confirmation creates an `app_user` row in
Aurora).

**Design references:**
- `design.md` §Components.User Onboarding Flow (sequence diagram).
- `design.md` §Components.Lambda Functions (the cognito module owns this
  Lambda; counted separately from the 13 business Lambdas).

---

## Behavior

1. Read `cognito_sub` (= `sub` attribute) and `email` from the Cognito
   event's `request.userAttributes`. Both are NOT NULL UNIQUE on
   `app_user`, so both are validated up-front and a missing or blank
   value raises `PostConfirmationError`.
2. Read Aurora connection parameters from environment variables injected
   by Terraform: `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`. **No static
   passwords** — authentication uses IAM database authentication via
   `boto3.client("rds").generate_db_auth_token` (15-min token).
3. Open a TLS-enabled `pg8000` connection to Aurora using the IAM token
   as the Postgres password.
4. Execute `INSERT INTO app_user (cognito_sub, email) VALUES (%s, %s)
   ON CONFLICT (cognito_sub) DO NOTHING`. The `ON CONFLICT` clause makes
   the operation idempotent against Cognito retries: a second invocation
   with the same `sub` produces `rowcount = 0` and does not raise.
5. Commit the transaction, close the connection, return the original
   event unchanged so Cognito completes the post-confirmation step.

On any unexpected failure (network, DB error, missing required event
fields), the function raises. Cognito treats the raise as a failure,
returns a non-fatal error to the user-facing flow, and retries up to
its built-in retry budget. Silently dropping `app_user` rows would
leave authenticated users with no Aurora identity, breaking every
downstream RLS-aware query — so the function deliberately does **not**
swallow errors.

---

## Optional `org_id` handling

The `custom:org_id` Cognito attribute is informational — the SAML
attribute mapping (or self-signup flow) may surface a pending
Organization affiliation. Migration 7.1 (Task 7.1) adds the governance
tables but does **not** currently include an `org_id` column on
`app_user`. To stay deployable today and ready for the column being
added later, this Lambda gates inclusion of `org_id` in the INSERT
statement on the `APP_USER_HAS_ORG_ID` env var:

| `APP_USER_HAS_ORG_ID` | INSERT shape |
|---|---|
| unset / `false` (default) | `INSERT INTO app_user (cognito_sub, email) VALUES (%s, %s) …` |
| `true` | `INSERT INTO app_user (cognito_sub, email, org_id) VALUES (%s, %s, %s) …` |

When migration 7.1 lands and the column is added, the dev composition
flips this env var to `true` without code change. Until then,
`custom:org_id` is read from the event but discarded — the user can
still claim the affiliation via the Web App's access-request flow.

---

## Environment variables

Injected by the `lambdas/post-confirmation` Terraform module
(supplied to the consuming environment composition).

| Variable | Required | Purpose |
|---|---|---|
| `DB_HOST` | yes | Aurora writer endpoint hostname (from `module.aurora.cluster_endpoint`). |
| `DB_PORT` | no (default `5432`) | Aurora port. |
| `DB_NAME` | yes | Database name (from `module.aurora.db_name`, default `biodata_registry`). |
| `DB_USER` | yes | DB user with `rds_iam` membership and INSERT on `app_user`. Created by the schema migration runner; the Terraform module passes the username here. |
| `AWS_REGION` | provided by Lambda runtime | Used for `generate_db_auth_token`. |
| `APP_USER_HAS_ORG_ID` | no (default `false`) | Set to `true` once migration 7.1 adds an `org_id` column to `app_user`. |
| `LOG_LEVEL` | no (default `INFO`) | Standard Python logging level. |
| `DB_CONNECT_TIMEOUT_SECONDS` | no (default `10`) | Connect timeout for the pg8000 connection. |

---

## Local development

The package is laid out so the Lambda entry point is
`services/post-confirmation-lambda/handler.py` and tests sit next to it
under `tests/`. Run the unit tests from the repo root or the package
root:

```bash
cd customers/NPO/RSC/Allen_Institute/biodata-registry/services/post-confirmation-lambda
python -m pytest -q
```

Tests use `unittest.mock` to stub the `boto3` RDS client and the
`pg8000` connection — they never hit a real Aurora cluster.

The unit tests cover the three behaviors required by Task 5.2:

1. Successful insert (one INSERT call with `(cognito_sub, email)` plus
   commit + close).
2. Idempotent re-trigger (second invocation with same event does not
   raise even when the row already exists, because the SQL uses
   `ON CONFLICT (cognito_sub) DO NOTHING`).
3. Missing email raises `PostConfirmationError` and never opens a DB
   connection.

Two property-based tests (Hypothesis) cover the same invariants over
arbitrary Cognito subs and email shapes.

---

## Packaging for Lambda

Terraform packages this directory plus the runtime deps from
`requirements.txt` into a deployment zip. The accompanying Terraform
module (Task 5.2 lives alongside this code at
`terraform/modules/lambdas/post-confirmation`) handles:

1. `pip install --target build/ -r requirements.txt`
2. Copying `handler.py` into `build/`
3. Producing `dist/post-confirmation.zip` via `archive_file`
4. Provisioning the `aws_lambda_function` resource pointing at the zip,
   wiring VPC config (so the Lambda can reach Aurora via the private
   subnets), and exporting `function_arn` for the cognito module to
   consume.

`pg8000` is pure-Python (no `libpq` native bindings), which keeps the
deployment package fully portable — `terraform apply` from any
developer workstation produces a working Lambda zip without
platform-specific wheels or Docker-based builds. `boto3` is provided by
the AWS Lambda Python 3.12 runtime, so it is intentionally not bundled.

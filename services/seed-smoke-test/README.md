# Seed Smoke Test Lambda — Allen BioData Registry PoC

A small Python Lambda that runs a fixed set of read-only SQL
assertions against Aurora *after* the seeder Lambda (Task 9.1) has
finished, and **fails the Terraform apply** when the seeded data is
missing or relationally inconsistent.

**Validates:** R2.7 (FK constraints prevent orphan references), R32.5
(idempotent `terraform apply` — a successful apply guarantees seeded
data is present and consistent).

**Design references:**
- `design.md` §Testing Strategy.E2E Tests.QC1
- `design.md` §IaC.Idempotency and Sample Data
- `services/seeder/README.md` (the writer this Lambda verifies).

---

## Why a separate Lambda

A silent seed failure is the worst-case bring-up bug: the seeder
Lambda returns 200, `terraform apply` reports success, the customer
walkthrough at QC1 begins, and OpenSearch turns out to be empty
because Aurora was never populated. The smoke test closes that gap
by making the apply itself fail when the seed produced no rows or
violated relational invariants.

The smoke test is intentionally **separate** from the seeder Lambda:

- Embedding the assertions inside the seeder would mean a failed
  assertion rolls back the whole seed transaction. We want the
  partial seed preserved so an operator can debug what was loaded.
- The seeder is a *writer* and the smoke test is a *verifier* —
  separating the two follows the audit-pattern "run the writer, then
  run a separate verifier".
- A future iteration that swaps the seeder for a different mechanism
  (e.g. AWS DMS) gets the smoke test for free.

Three options were considered for the verifier itself:

- **Option A — Standalone Lambda invoked after the seeder** (this
  directory). Reuses the IAM-DB-auth + pg8000 + Terraform-invoked
  pattern already established by the migration-runner and seeder
  Lambdas. **Chosen.** Minimum new infrastructure; "just works" from
  a CI runner with no operator-side prerequisites.
- **Option B — Bash script using `psql`, run from a bastion / SSM
  session post-apply.** Rejected: requires operator VPC reach + an
  out-of-band invocation step; defeats the whole "fail the apply"
  contract.
- **Option C — Python script under `scripts/seed_smoke_test.py`
  invoked via `local-exec` from Terraform.** Rejected for the same
  reason the seeder is a Lambda: Aurora is in private subnets, no
  reach from a developer laptop or CI runner.

---

## What the smoke test asserts

On each invocation the Lambda runs every assertion (so the operator
sees ALL failures, not just the first), aggregates the results, and
either returns a structured summary or raises `SmokeTestFailed` so
Terraform fails the apply.

### Row-count thresholds

1. `data_asset` row count >= `min_data_assets` (default `10`).
2. `subject` row count >= `min_subjects` (default `1`).
3. `instrument` row count >= `min_instruments` (default `1`).
4. `session` row count >= `min_sessions` (default `1`).

The 10/1/1/1 defaults are conservatively below what a 10% sample of
the customer's snapshot produces (~10k records → ~10k Data_Assets
each with at least one Subject/Instrument/Session), so the test
still passes when the operator runs against a smaller sub-sample for
development.

### FK orphan checks (defense in depth)

The seeder uses real FKs with `REFERENCES` clauses (migrations 0002
and 0003), so a true orphan would have failed at INSERT time. The
smoke test therefore measures **defense in depth** — a future
iteration that disables FKs for bulk-load speed, or a migration that
accidentally drops a constraint, would surface here.

| Child table | FK column | Parent table |
|---|---|---|
| `session` | `data_asset_id` | `data_asset` |
| `acquisition` | `data_asset_id` | `data_asset` |
| `processing` | `data_asset_id` | `data_asset` |
| `quality_control` | `data_asset_id` | `data_asset` |
| `data_description` | `data_asset_id` | `data_asset` |
| `data_asset_subject` | `data_asset_id` | `data_asset` |
| `data_asset_subject` | `subject_id` | `subject` |
| `data_asset_instrument` | `data_asset_id` | `data_asset` |
| `data_asset_instrument` | `instrument_id` | `instrument` |

NULL FK values are excluded — those rows are not orphans, they just
don't reference a parent (e.g. `session.subject_id` is nullable per
migration 0002 because not every session has a subject).

### Defaulted-column NULL check

Every `data_asset` row should have non-NULL values in
`lifecycle_state`, `validation_status`, `space_id`, `created_by`, and
`storage_uri`. The columns are NOT NULL with defaults in migration
0002, so NULL here would mean someone disabled the default — defense
in depth again.

### Bootstrap-row existence

The seeder bootstraps three rows under stable natural keys:

- `app_user` with `cognito_sub = 'system-seeder'`.
- `organization` with `name = 'system'`.
- `space` with `name = 'default-space'` under that org.

Their absence would mean the bootstrap step was skipped, which would
orphan every `created_by` FK in the seeded data.

---

## RLS and the "smoke test sees all rows" guarantee

The smoke test connects as `migration_runner`, which has
`rds_superuser` membership and therefore `BYPASSRLS`. That alone is
enough to see every row regardless of governance state.

We additionally execute on each connection:

```sql
SET row_security = off;
SET app.current_user_role_set = 'data_administrator';
```

The `SET row_security = off` is redundant when the user already has
BYPASSRLS but cheap and defensive. The role-set GUC is the input to
migration 0006_rls_policies.sql's `is_data_admin()` helper, which
short-circuits the restrictive sensitive-flag policy and bypasses
the per-table transitive-visibility predicates. If a future
iteration drops the smoke test to a non-superuser DB role, BYPASSRLS
will be lost and these GUCs become the visibility predicate —
setting them now means that future change is a one-line role-rename
rather than "rewrite the smoke test".

---

## DB user choice

Same as the seeder: `migration_runner`. The smoke test is a read-
only verifier so it doesn't need the seeder's INSERT privileges —
strictly it only needs `SELECT` on every registry table. Splitting
to a dedicated `smoke_test_runner` role (with read-only grants)
would be a strict improvement for production but adds an
out-of-band role-creation step for the PoC. The IAM-policy scoping
in the Lambda's execution role still limits the Lambda to a single
`{cluster_resource_id, db_user}` tuple — same blast-radius
mitigation as the migration-runner and seeder.

Production should split the role once per-table grants stabilise.

---

## Environment variables

Injected by the `lambdas/seed-smoke-test` Terraform module.

| Variable | Required | Purpose |
|---|---|---|
| `DB_HOST` | yes | Aurora writer endpoint hostname (from `module.aurora.cluster_endpoint`). |
| `DB_PORT` | no (default `5432`) | Aurora port. |
| `DB_NAME` | yes | Database name. |
| `DB_USER` | yes | DB user (default `migration_runner`). |
| `MIN_DATA_ASSETS` | no (default `10`) | Minimum row count required in `data_asset`. |
| `MIN_SUBJECTS` | no (default `1`) | Minimum row count required in `subject`. |
| `MIN_INSTRUMENTS` | no (default `1`) | Minimum row count required in `instrument`. |
| `MIN_SESSIONS` | no (default `1`) | Minimum row count required in `session`. |
| `AWS_REGION` | provided by Lambda runtime | Used for `generate_db_auth_token`. |
| `LOG_LEVEL` | no (default `INFO`) | Standard Python logging level. |
| `DB_CONNECT_TIMEOUT_SECONDS` | no (default `10`) | Connect timeout for the pg8000 connection. |

The Lambda timeout defaults to 60s — every check is a single SELECT
COUNT/EXISTS, the whole suite finishes in well under a second
against a freshly-seeded cluster.

---

## Local development

```
services/seed-smoke-test/
├── handler.py            # Lambda entry point
├── smoke_test.py         # Core assertions
├── requirements.txt      # pg8000 only (boto3 is provided by the runtime)
├── pyproject.toml        # Test/dev tooling configuration
├── README.md             # This file
└── tests/
    ├── __init__.py
    ├── conftest.py
    ├── test_handler.py    # Lambda framing tests (env vars, IAM, conn)
    └── test_smoke_test.py # Per-check correctness, all-pass, aggregate-fail
```

Run the unit tests:

```bash
cd customers/NPO/RSC/Allen_Institute/biodata-registry/services/seed-smoke-test
python -m pytest -q
```

Tests use `unittest.mock` + a stateful `FakeConn` to exercise the
smoke test logic without touching a real Aurora cluster. The four
behaviours required by the task brief are all covered:

1. **Each assertion correctly identifies the failure mode** — every
   check has a paired pass / fail test.
2. **All-pass case returns success** — happy path returns a summary
   with `passed=True` and no `SmokeTestFailed` raised.
3. **First-fail-and-aggregate** — when multiple checks fail, every
   failure is recorded in `summary.checks` and the raise happens
   exactly once, after all checks have run.
4. **Lambda framing** — env-var validation, IAM token mint,
   connection close-on-error all match the migration-runner / seeder
   pattern.

---

## Packaging for Lambda

Terraform packages this directory plus the runtime deps from
`requirements.txt` into a deployment zip. The accompanying Terraform
module (`terraform/modules/lambdas/seed-smoke-test`) handles:

1. `pip install --target build/ -r requirements.txt`
2. Copying `handler.py`, `smoke_test.py` into `build/`
3. Producing `dist/seed_smoke_test.zip` via `archive_file`
4. Provisioning the `aws_lambda_function` resource pointing at the
   zip, wiring VPC config (so the Lambda can reach Aurora via the
   private subnets), and exposing both the function ARN (for
   diagnostics) and an `aws_lambda_invocation` data source that runs
   the function on every `terraform apply`. The invocation has an
   explicit `depends_on = [module.seeder]` edge so it always runs
   AFTER the seeder.

`pg8000` is pure-Python (no `libpq` native bindings), which keeps the
deployment package fully portable — `terraform apply` from any
developer workstation produces a working Lambda zip without
platform-specific wheels or Docker-based builds.

---

## Operational caveats

- **Read-only:** every query is a `SELECT` (no INSERT / UPDATE /
  DELETE), so a re-invocation is safe at any time.
- **Idempotent failure:** if the smoke test fails, the operator can
  re-run the seeder (which is itself idempotent) and re-apply
  Terraform to retry the smoke test. There is no "half-checked"
  state.
- **Privilege:** the `migration_runner` DB user is effectively
  superuser. Compromise of an IAM-auth token would let an attacker
  read every row in the registry. Mitigations: 15-minute token TTL,
  IAM scoping to one `{cluster_resource_id, db_user}` tuple, VPC-
  only invocation, the Lambda is invoked only by Terraform.
- **Threshold tuning:** the `MIN_*` defaults are conservative to
  cover smaller dev sub-samples; production should bump them to
  realistic floor values (e.g. ~5000 for `MIN_DATA_ASSETS` against
  a 10% sample) so a partial-seed failure is still caught.
- **Schema dependence:** the assertions reference table and column
  names from migrations 0001–0007. A schema rename in a future
  migration must be mirrored here (and the bootstrap-key constants
  must stay in sync with `services/seeder/seeder.py`).

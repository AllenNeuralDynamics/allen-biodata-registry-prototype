# Schema Migration Runner Lambda — Allen BioData Registry PoC

A small Python Lambda that applies the Aurora schema migrations under
`customers/.../biodata-registry/migrations/` in lexical order, tracking
applied versions in a `schema_version` table. Invoked by Terraform (via
`aws_lambda_invocation`) after the `aurora` module brings up the
cluster, so `terraform apply` is the single command that gets the
registry from "no infrastructure" to "schema deployed".

**Validates:** R32.5 (idempotent `terraform apply`).

**Design references:**
- `design.md` §IaC.Idempotency and Sample Data.
- `migrations/README.md` (runner contract — filename convention, the
  `-- +runner: no-transaction` directive, forward-only convention).

---

## Why a Lambda runner instead of a local-exec script

Two approaches were considered (see Task 8.1 brief):

**Option A — Lambda runner** (this directory). Reuses the IAM-DB-auth
+ pg8000 pattern already established by the Cognito Post-Confirmation
Lambda. Runs from inside the VPC, so there is no operator-side
prerequisite — `terraform apply` from a developer laptop or CI runner
"just works" against an Aurora cluster sitting in private subnets.

**Option B — Local-exec script**. A plain Python script invoked by a
`null_resource` `local-exec` provisioner. Simpler in terms of moving
parts but requires the operator to have direct VPC reach to Aurora
(Cloud9, AWS Client VPN, or an SSM port-forward tunnel) — which is not
available on every workstation and is awkward to orchestrate in CI.

**Option A was chosen** because operator-VPC-reach is uncertain and
the Lambda approach leverages infrastructure (post-confirmation
Lambda's packaging/IAM pattern) that already exists for this project.

---

## What the runner does

On each invocation:

1. Connects to Aurora using IAM database authentication. The DB user
   is the privileged `migration_runner` role (see "Required DB user"
   below).
2. Issues `CREATE TABLE IF NOT EXISTS schema_version (...)` — bookkeeping
   only; does nothing if the table already exists.
3. Discovers every `*.sql` file under the configured migrations
   directory (default `/var/task/migrations`) in **lexical order**.
4. For each file:
   - Computes the **SHA-256 checksum** of the file's bytes.
   - Extracts the version from the filename's leading numeric prefix
     (e.g. `0001_governance.sql` → `0001`).
   - Looks up the version in `schema_version`:
     - **Missing** — reads the file, executes the SQL inside a single
       transaction (`BEGIN`/`COMMIT`), then INSERTs a `schema_version`
       row in the same transaction. If the file's first 100 chars
       contain the directive `-- +runner: no-transaction`, the SQL is
       run in autocommit mode instead.
     - **Present** — compares the stored checksum to the recomputed
       one. **Different** → emit a CRITICAL log warning (drift) but do
       NOT re-apply (forward-only convention).
5. Detects out-of-order discovery (a new file with a version less than
   the maximum already-applied version) and raises
   `MigrationOrderError` before applying anything.
6. Returns a structured summary:

   ```json
   {
     "applied":                ["0001_governance.sql", "..."],
     "skipped":                ["0002_data_asset.sql", "..."],
     "drift":                  [{"version": "0003", "filename": "...", ...}],
     "schema_version_created": true,
     "elapsed_ms":             1234
   }
   ```

---

## `schema_version` table

Created by the runner on first invocation:

```sql
CREATE TABLE schema_version (
  version    text PRIMARY KEY,
  filename   text NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT now(),
  checksum   text,
  applied_by text
);
```

**Note:** this is intentionally a different table from
`schema_definition` (created by `0005_collections_schemas.sql`), which
holds *application-level* Biodata_Schema versions.

---

## Required DB user (`migration_runner`)

The Lambda authenticates as a privileged Postgres role — typically
named `migration_runner` — that must:

- Have membership in the `rds_iam` Aurora role so IAM database
  authentication is enabled.
- Have membership in `rds_superuser` (Aurora's superuser-equivalent)
  so it can:
  - `CREATE EXTENSION` for `citext`, `pgcrypto`, `vector`, `pg_trgm`
    (required by 0001/0002).
  - `CREATE TABLE` / `CREATE TYPE` / `CREATE INDEX` /
    `ALTER TABLE` on the public schema.
  - `GRANT` privileges to per-Lambda DB users (the 0006 RLS migration
    issues GRANTs).
  - `CREATE POLICY` / `ALTER TABLE … ENABLE ROW LEVEL SECURITY` (the
    0006 migration uses these).

Aurora's bootstrap (Task 3.1's `null_resource.bootstrap_slot_and_extensions`
or, longer term, a dedicated bootstrap step) is responsible for
**creating** this role; the migration runner only **consumes** it.

Operationally this means the migration runner has high blast radius —
it can do anything to the schema. We accept that for the PoC because
(a) it runs only at apply time, (b) the IAM scoping limits its reach
to one specific `{cluster_resource_id, db_user}` tuple, and (c) IAM
DB-auth tokens are 15-minute-TTL, so even a leaked token is short-lived.

---

## Environment variables

Injected by the `lambdas/migration-runner` Terraform module.

| Variable | Required | Purpose |
|---|---|---|
| `DB_HOST` | yes | Aurora writer endpoint hostname (from `module.aurora.cluster_endpoint`). |
| `DB_PORT` | no (default `5432`) | Aurora port. |
| `DB_NAME` | yes | Database name (from `module.aurora.db_name`, default `biodata_registry`). |
| `DB_USER` | yes | DB user the Lambda authenticates as. Must have `rds_iam` membership and the privileges listed above. Default in the Terraform module: `migration_runner`. |
| `MIGRATIONS_DIR` | no (default `/var/task/migrations`) | Override the directory the runner reads `.sql` files from. Useful for local testing; production invocations rely on the bundled default. |
| `AWS_REGION` | provided by Lambda runtime | Used for `generate_db_auth_token`. |
| `LOG_LEVEL` | no (default `INFO`) | Standard Python logging level. |
| `DB_CONNECT_TIMEOUT_SECONDS` | no (default `10`) | Connect timeout for the pg8000 connection. |

The runner timeout should be generous (the Terraform module defaults
to 300 seconds) — applying the registry's seven migrations on a
freshly-provisioned Aurora cluster typically takes 30–90 seconds, but
cold-start network setup and Aurora's first-connection latency can add
another 15–30 seconds.

---

## Local development

The package layout mirrors the post-confirmation Lambda:

```
services/migration-runner/
├── handler.py            # Lambda entry point
├── runner.py             # Core algorithm (unit-testable in isolation)
├── requirements.txt      # pg8000 only (boto3 is provided by the runtime)
├── pyproject.toml        # Test/dev tooling configuration
├── README.md             # This file
└── tests/
    ├── __init__.py
    ├── conftest.py
    ├── test_runner.py    # Algorithm unit tests
    └── test_handler.py   # Lambda framing tests
```

Run the unit tests:

```bash
cd customers/NPO/RSC/Allen_Institute/biodata-registry/services/migration-runner
python -m pytest -q
```

Tests use `unittest.mock` + a stateful in-memory `FakeConn` to exercise
the runner algorithm without touching a real Aurora cluster.

The unit tests cover the four behaviors required by Task 8.1:

1. **First invocation creates `schema_version` + applies all 7 migrations.**
2. **Second invocation is a no-op.**
3. **Modified file (different checksum) is reported as drift but NOT re-applied.**
4. **Out-of-order discovery raises `MigrationOrderError`.**

Plus property-based tests (Hypothesis) over arbitrary filename corpora
asserting lexical-order apply + idempotency.

---

## Packaging for Lambda

Terraform packages this directory plus the runtime deps from
`requirements.txt` plus a copy of `migrations/*.sql` into a deployment
zip. The accompanying Terraform module
(`terraform/modules/lambdas/migration-runner`) handles:

1. `pip install --target build/ -r requirements.txt`
2. Copying `handler.py` and `runner.py` into `build/`
3. Copying every `*.sql` file from `migrations/` into
   `build/migrations/` so the Lambda finds them at
   `/var/task/migrations/` at runtime
4. Producing `dist/migration-runner.zip` via `archive_file`
5. Provisioning the `aws_lambda_function` resource pointing at the zip,
   wiring VPC config (so the Lambda can reach Aurora via the private
   subnets), and exposing both the function ARN (for diagnostics) and
   an `aws_lambda_invocation` data source that runs the function on
   every `terraform apply`.

`pg8000` is pure-Python (no `libpq` native bindings), which keeps the
deployment package fully portable — `terraform apply` from any
developer workstation produces a working Lambda zip without
platform-specific wheels or Docker-based builds.

The deployment zip's expected size is **roughly 600–800 KB**: pg8000
itself is ~170 KB, the seven SQL migrations total ~50 KB, and our
`handler.py` + `runner.py` add another ~25 KB. boto3 is provided by
the AWS Lambda Python 3.12 runtime, so it is intentionally not bundled.

---

## Operational caveats

- **Privilege:** The `migration_runner` DB user is effectively
  superuser within `biodata_registry`. Compromise of its IAM-auth
  token would let an attacker drop tables or rewrite RLS policies.
  Mitigations: 15-minute token TTL, IAM scoping to one
  `{cluster_resource_id, db_user}` tuple, VPC-only invocation, and the
  Lambda is invoked only by Terraform.
- **Drift is silent at runtime:** drift logs at CRITICAL but is **not**
  a failed apply. Operators must inspect CloudWatch Logs after every
  apply that touches an existing migration. The forward-only convention
  is the contract; drift is the safety net.
- **No-transaction migrations are riskier:** if a `-- +runner:
  no-transaction` migration fails halfway through, the database is
  partially-applied and `schema_version` has no record of the attempt.
  Authors of no-transaction migrations are responsible for making the
  SQL itself idempotent. The registry has zero such migrations today.
- **Out-of-order recovery:** if `MigrationOrderError` fires, the fix
  is to **rename the offending file** to a version greater than the
  maximum already-applied version. Do not delete the
  `schema_version` rows to "make room" — that breaks the audit trail
  and risks re-applying migrations.
- **Long-running migrations:** if a migration takes longer than the
  Lambda timeout (default 300s), the Lambda is killed but the SQL may
  continue inside Aurora until it finishes. The next invocation will
  see the rows already created and treat the file as drift if the
  `schema_version` INSERT did not commit. Mitigation: bump
  `var.timeout_seconds` for migrations expected to be slow, and
  prefer breaking large migrations into multiple smaller files.

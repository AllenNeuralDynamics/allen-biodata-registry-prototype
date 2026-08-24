# Sample-Data Seeder Lambda — Allen BioData Registry PoC

A small Python Lambda that streams a 10% sample of the
aind-data-schema snapshot from S3 and inserts the records into Aurora
through the relational data-asset + shared-entity graph defined by
migrations 0001–0007. Invoked by Terraform (via
`aws_lambda_invocation`) once the migration runner has applied the
schema, so `terraform apply` is the single command that gets the
registry from "no infrastructure" to "schema deployed and seeded".

**Validates:** R32.2 (sample data loaded), R32.5 (idempotent
`terraform apply`).

**Design references:**
- `design.md` §IaC.Idempotency and Sample Data
- `design.md` §Effort Estimation.Data Seeding
- `services/migration-runner/README.md` (sibling Lambda whose
  packaging + IAM-DB-auth pattern this Lambda mirrors).

---

## Why a Lambda seeder instead of a local-exec script

Two approaches were considered:

**Option A — Lambda seeder** (this directory). Reuses the IAM-DB-auth
+ pg8000 pattern already established by the migration-runner Lambda.
Runs from inside the VPC, so there is no operator-side prerequisite —
`terraform apply` from a developer laptop or CI runner "just works"
against an Aurora cluster sitting in private subnets.

**Option B — Local-exec script**. A plain Python script invoked by a
`null_resource` `local-exec` provisioner. Simpler in terms of moving
parts but requires the operator to have direct VPC reach to Aurora
(Cloud9, AWS Client VPN, or an SSM port-forward tunnel) — which is
not available on every workstation and is awkward to orchestrate in
CI.

**Option A was chosen** because operator-VPC-reach is uncertain and
the Lambda approach reuses infrastructure that already exists for
this project (the migration-runner's packaging/IAM pattern).

---

## What the seeder does

On each invocation:

1. Connects to Aurora using IAM database authentication. The DB user
   is `migration_runner` (see "DB user choice" below).
2. **Bootstraps a `system` principal** (idempotent via UPSERT):
   - Organization: `name='system'`.
   - App user:    `cognito_sub='system-seeder'`,
                  `email='system-seeder@biodata-registry.local'`.
   - Space:       `name='default-space'` under the system org.
   The seeder grabs the resulting ids back via `RETURNING id` so it
   can attribute every seeded row.
3. **Streams the snapshot from S3.** The seeder probes the first
   non-whitespace byte to choose between JSON-array and NDJSON
   parsers. The 10% sample (~700 MB) fits comfortably in 1 GB Lambda
   memory at `json.loads`; production scale-up to the full 7 GB
   snapshot would need a streaming parser (see
   `requirements.txt`).
4. **For each record:**
   - Computes a SHA-256 content hash of the canonicalised JSON.
   - Applies the **deterministic-modulo sampling filter** so the
     same fraction of records is selected on every run. (See
     "Sampling determinism" below.)
   - Calls `mapping.map_record` to derive the relational rows.
   - Issues parameterised INSERTs into Aurora wrapped in a
     per-record transaction with `ON CONFLICT DO NOTHING` (or
     `DO UPDATE` for shared entities) so re-runs are pure no-ops.
   - Stamps the four junction tables (`data_asset_subject`,
     `data_asset_instrument`, `data_asset_rig`,
     `data_asset_procedures`) once it has both the asset id and the
     shared-entity ids.
5. **Returns a structured summary** describing what was processed
   and what was inserted.

---

## DB user choice

The seeder authenticates as **`migration_runner`** (the same DB user
the migration-runner Lambda uses). Rationale:

- The seeder is also a bring-up-time tool and needs INSERT privileges
  on every registry table, plus the bootstrap path needs INSERT on
  `organization`, `space`, and `app_user`. Listing every table in a
  dedicated `seeder_runner` role's grant set adds operational
  complexity without security benefit at PoC scale.
- `migration_runner` already has the necessary privileges via its
  `rds_superuser` membership.
- IAM scoping in the Lambda's execution role still limits the
  Lambda to a single `{cluster_resource_id, db_user}` tuple — same
  blast-radius mitigation as the migration-runner.
- Production should split the roles once the registry has stable
  per-table grants. The seeder would then drop to an INSERT-only
  user with row-level grants tied to the system space.

---

## Sampling determinism (R32.5)

Re-running the seeder must produce zero new INSERTs. The seeder
guarantees this at three levels:

1. **Sampling**  — `mapping.should_sample` takes the SHA-256 content
   hash and returns the same accept/reject decision every time. The
   first 8 hex chars (32 bits) modulo 1,000,000 are compared against
   `fraction * 1,000,000`, giving a per-record acceptance probability
   matching `fraction` to within 1 ppm. Same record → same digest →
   same decision. No RNG, no global state, no shuffling.
2. **Asset-first ordering** — for each sampled record, the seeder
   tries to insert the `data_asset` row **first** with `ON CONFLICT
   (storage_uri) DO NOTHING RETURNING id`. If the asset already
   exists, the rest of the record (shared entities, asset-specific
   children, junctions) is skipped entirely. This is the critical
   idempotency invariant: shared-entity tables are never touched on
   a re-run, and asset-specific tables (which lack UNIQUE
   constraints) cannot accumulate duplicates.
3. **On-fresh-asset inserts** — when the Data_Asset DOES insert
   freshly (first run, or a record that was just sampled in for the
   first time), shared entities use `INSERT ... ON CONFLICT
   (<natural_key>) DO UPDATE SET ... RETURNING id` so we get back
   the existing id whether the entity already exists from another
   record in this run or is freshly minted. Junctions use composite
   PK + `ON CONFLICT DO NOTHING` (defensive — only reachable on
   fresh-asset path).

The bootstrap path (`organization`, `space`, `app_user`) is also
idempotent via the same `ON CONFLICT DO UPDATE RETURNING id` shape.

---

## Field-mapping decisions

The aind-data-schema record format on disk is a 7 GB JSON corpus the
customer provided. Its exact field-by-field shape is not formally
documented in this repo — the design doc treats it as a black box. The
seeder is therefore **best-effort** about field mapping:

- For each promoted column, the mapper looks in **several plausible
  field locations** (e.g. `record["storage_uri"]`,
  `record["location"]["s3_uri"]`, `record["location"]` when it's a
  string, `record["s3_uri"]`). The first non-empty hit wins.
- **Missing optional fields** map to `NULL`.
- **Required fields without a plausible source** are filled with
  safe placeholders so the row inserts cleanly:
  - `data_asset.storage_uri` → `seed://no-storage-uri/<content_hash>`
    (sentinel scheme so the customer can grep the seeded data for
    "no real URI" cases).
  - `subject.subject_id` → `seed-subject-<hash[:16]>`.
  - `instrument.instrument_id` → `seed-instrument-<hash[:16]>`.
  - `rig.rig_id` → `seed-rig-<hash[:16]>`.
  - `subject.species` → `'unknown'`.
- The **full source record** (modulo top-level fields already
  promoted to columns) is preserved in the `metadata` JSONB column
  on every entity, under the `source_record` key. The seeder also
  stamps a `__seeder` metadata block with the content hash for
  re-run debugging.
- aind-data-schema validation errors are **not** enforced by the
  seeder; rows land with `validation_status = 'unvalidated'` and the
  Validation_Lambda (Task 21) re-validates them at QC1. This is
  intentional: the seeder is bring-up scaffolding, not the validation
  pipeline.

The mapping decisions are documented per-function in `mapping.py`.
The customer should treat the seeded data as PoC scaffolding and
re-validate the field-by-field mappings against the real corpus
before relying on it for QC1 demos.

---

## Asset-specific entity 1:1 simplification

The five asset-specific entities (`session`, `acquisition`,
`processing`, `quality_control`, `data_description`) are tied to a
single Data_Asset per design.md §Overview.Guiding Principles +
Property 10. The seeder emits **at most one row per kind per
record**; if the source carries multiple sessions we keep the first
and stash the rest into the Data_Asset's `metadata` blob.

This is a deliberate simplification — fanning out to multiple rows
per record is straightforward but adds complexity (more INSERTs,
more failure modes) the PoC does not need. A future iteration can
remove the simplification once the customer validates the QC1 demo
shape against the real snapshot.

---

## Environment variables

Injected by the `lambdas/seeder` Terraform module.

| Variable | Required | Purpose |
|---|---|---|
| `DB_HOST` | yes | Aurora writer endpoint hostname (from `module.aurora.cluster_endpoint`). |
| `DB_PORT` | no (default `5432`) | Aurora port. |
| `DB_NAME` | yes | Database name. |
| `DB_USER` | yes | DB user (default `migration_runner`). |
| `SEED_S3_BUCKET` | yes | S3 bucket of the snapshot (e.g. `aind-scratch-data`). |
| `SEED_S3_KEY` | yes | S3 key (e.g. `jon.young/metadata_v2_records_20260324/data_assets.json`). |
| `SEED_SAMPLE_FRACTION` | no (default `0.1`) | Fraction in (0.0, 1.0] to seed. Deterministic per record. |
| `AWS_REGION` | provided by Lambda runtime | Used for `generate_db_auth_token` + S3 client region. |
| `LOG_LEVEL` | no (default `INFO`) | Standard Python logging level. |
| `DB_CONNECT_TIMEOUT_SECONDS` | no (default `10`) | Connect timeout for the pg8000 connection. |

The Lambda timeout should be the maximum (15 minutes / 900s). Even
the 10% sample (~10k records, ~700 MB) takes 5–15 minutes when each
record fans out to 10+ INSERTs across 14 tables. The Terraform module
defaults to `timeout_seconds = 900`.

---

## Local development

The package layout mirrors the migration-runner Lambda:

```
services/seeder/
├── handler.py            # Lambda entry point
├── seeder.py             # Core algorithm (orchestration + DB writes)
├── mapping.py            # Pure-functional record → row mapping
├── requirements.txt      # pg8000 only (boto3 is provided by the runtime)
├── pyproject.toml        # Test/dev tooling configuration
├── README.md             # This file
└── tests/
    ├── __init__.py
    ├── conftest.py
    ├── fixtures/
    │   └── sample_records.json
    ├── test_mapping.py    # Per-entity mapper unit tests + property tests
    ├── test_seeder.py     # Algorithm + idempotency + sampling tests
    └── test_handler.py    # Lambda framing tests (env vars, IAM, S3, conn)
```

Run the unit tests:

```bash
cd customers/NPO/RSC/Allen_Institute/biodata-registry/services/seeder
python -m pytest -q
```

Tests use `unittest.mock` + a stateful in-memory `FakeConn` to
exercise the seeder algorithm without touching a real Aurora cluster
or a real S3 bucket. The S3 client is replaced with a stub that
returns a small fixture file. The unit tests cover the four
behaviours required by Task 9.1:

1. **Mapping logic produces correct INSERTs for a 5-record fixture.**
2. **Idempotency** — re-running against the same fixture produces
   zero new INSERTs.
3. **Sampling fraction is deterministic** — same hash modulo gives
   same subset.
4. **Missing optional fields don't crash** — degenerate records still
   land cleanly.

Plus property-based tests (Hypothesis) over arbitrary record shapes
asserting `should_sample` is deterministic and `map_record` never
raises.

---

## Packaging for Lambda

Terraform packages this directory plus the runtime deps from
`requirements.txt` into a deployment zip. The accompanying Terraform
module (`terraform/modules/lambdas/seeder`) handles:

1. `pip install --target build/ -r requirements.txt`
2. Copying `handler.py`, `seeder.py`, `mapping.py` into `build/`
3. Producing `dist/seeder.zip` via `archive_file`
4. Provisioning the `aws_lambda_function` resource pointing at the
   zip, wiring VPC config (so the Lambda can reach Aurora via the
   private subnets), and exposing both the function ARN (for
   diagnostics) and an `aws_lambda_invocation` data source that runs
   the function on every `terraform apply`.

`pg8000` is pure-Python (no `libpq` native bindings), which keeps the
deployment package fully portable — `terraform apply` from any
developer workstation produces a working Lambda zip without
platform-specific wheels or Docker-based builds.

---

## Operational caveats

- **First-run runtime is long.** The 10% sample takes 5–15 minutes
  end-to-end. Lambda's hard 15-minute ceiling is the budget; if the
  sample grows or the fan-out per record increases, switch to a
  chunked invocation pattern (SQS-driven workers reading the snapshot
  in slices). For PoC volume the single-invocation pattern is fine.
- **Privilege:** the `migration_runner` DB user is effectively
  superuser within `biodata_registry`. Compromise of an IAM-auth
  token would let an attacker INSERT/UPDATE schema. Mitigations: 15-
  minute token TTL, IAM scoping to one `{cluster_resource_id,
  db_user}` tuple, VPC-only invocation, the Lambda is invoked only
  by Terraform.
- **Source data fidelity:** field mappings are best-effort against an
  undocumented record shape. Re-validate against the real snapshot
  before relying on the seeded data for QC1 demos. The full source
  record is preserved in `metadata.source_record`, so missed field
  mappings can be backfilled retroactively without re-running the
  seeder.
- **Errors are soft up to a budget.** The seeder records the first
  50 per-record errors in the summary and continues; once the budget
  is exhausted it raises `SeederError` and the Lambda fails the apply.
  This avoids producing a half-loaded database when the snapshot has
  systematic shape problems.
- **Smoke test is a separate task** (Task 9.2). The seeder reports
  insert counts in its summary, but a downstream check should
  validate FK consistency and minimum row counts before declaring
  QC1 ready.

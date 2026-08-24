# Registration Lambda — Allen BioData Registry PoC

Core CRUD Lambda for Data_Assets and Metadata_Entities. Sits behind
API Gateway and serves these routes:

| Method | Path                       | Behavior                                                    |
|--------|----------------------------|-------------------------------------------------------------|
| POST   | `/assets`                  | Create a Data_Asset; append `entity_revision` row.          |
| GET    | `/assets/{id}`             | RLS-filtered read; 404 when hidden, 403 on sensitive flag.  |
| PUT    | `/assets/{id}`             | Update; append a revision with monotonic `revision_number`. |
| POST   | `/entities/{type}`         | Create a polymorphic Metadata_Entity.                       |
| GET    | `/entities/{type}/{id}`    | Read with the same RLS / sensitive-flag rules as `/assets`. |
| PUT    | `/entities/{type}/{id}`    | Update; append revision with diff payload.                  |

**Validates:** R1.1, R1.2, R1.4, R1.5, R1.6, R1.7, R2.4, R2.5, R2.6,
R6.1, R6.2, R28.2, R33.1, R33.2.

**Design references:**
- `design.md` §Components.2. Registration_Lambda.
- `design.md` §Architecture.RLS Enforcement Architecture.
- `design.md` §External Interfaces.API Gateway REST.

---

## Behavior

### Writes are revision-anchored

Every successful POST/PUT writes exactly one immutable
`entity_revision` row in the same Aurora transaction as the table
INSERT/UPDATE. The revision carries:

- `entity_type` / `entity_id` — the polymorphic target.
- `revision_number` — monotonic per entity, computed via
  `COALESCE(MAX(revision_number), 0) + 1`. The
  `UNIQUE (entity_type, entity_id, revision_number)` constraint from
  migration 0004 serves as the backstop if two transactions race.
- `change_source` — derived from request headers:
  - `X-Agent-Source: true` → `agent` (Agent UI proxy).
  - `X-API-Source: true`   → `api` (programmatic third-party).
  - Otherwise              → `manual` (interactive UI write).
- `metadata_snapshot` — the full row JSON post-write.
- `previous_values` / `new_values` — diff payload. NULL on create.

The `entity_revision` table has `REVOKE UPDATE, DELETE FROM PUBLIC`
(see migration 0004), so revisions are append-only by construction —
the immutability invariant doesn't depend on application correctness.

### CDC, not dual-writes

The Lambda **does not** write to DocumentDB or OpenSearch directly.
Aurora's logical replication slot (`biodata_cdc`) is consumed by the
CDC pipeline (Task 17, Indexing_Lambda Task 18) which fans the change
out to both read stores asynchronously. The eventual-consistency
window (~5s p99) is documented in `design.md` §Architecture.CDC
Pipeline Architecture.

### Three-layer RLS

- **Layer 1 (Application)** — `require_space_access` from the shared
  Layer rejects writes to spaces the caller has no role on, returning
  403 `FORBIDDEN` before opening Aurora.
- **Layer 2 (Database)** — `aurora_connection` issues
  `SET LOCAL app.current_user_id`,
  `SET LOCAL app.current_org_ids`,
  `SET LOCAL app.current_space_ids`, and
  `SET LOCAL app.current_user_role_set` before any query runs.
  Migration 0006's policies key off these GUCs.
- **Layer 3 (API)** — `check_sensitive_flag` is invoked on every
  direct GET / row-touch path; non-privileged callers see
  403 `SENSITIVE_ACCESS_DENIED` instead of the row.

### Validation_Lambda is separate

This Lambda preserves whatever `validation_status` is currently
persisted (or sets `unvalidated` on insert by virtue of the table
default). The `POST /validate` endpoint (Validation_Lambda, Task 21)
is the single owner of `validation_status` mutations.

### Duplicates_Lambda is separate

Synchronous similarity-based duplicate detection (Duplicates_Lambda,
Task 25.1) is not yet wired here; for now the only 409
`DUPLICATE_ENTITY` path is the database unique-constraint backstop on
`data_asset_storage_uri_unique`. The 201 response always carries an
empty `warnings: []` array so the wire shape is forward-compatible
once Task 25 lands.

---

## Environment variables

Injected by the `lambdas/registration` Terraform module.

| Variable | Required | Purpose |
|---|---|---|
| `DB_HOST` | yes | Aurora writer endpoint. |
| `DB_PORT` | no (default `5432`) | Aurora port. |
| `DB_NAME` | yes | Database name. |
| `DB_USER` | yes | DB user with `rds_iam` membership and INSERT/UPDATE/SELECT on the registry tables. |
| `DB_SSLMODE` | no (default `require`) | psycopg SSL mode. |
| `DB_CONNECT_TIMEOUT_SECONDS` | no (default `10`) | TCP/TLS handshake timeout. |
| `DB_STATEMENT_TIMEOUT_MS` | no (default `10000`) | Per-statement timeout via `SET LOCAL statement_timeout`. |
| `OPENAPI_SPEC_PATH` | no | Path to the OpenAPI YAML inside the deployment package. Defaults to `openapi.yaml` next to `handler.py`. |
| `LOG_LEVEL` | no (default `INFO`) | Python logging level. |
| `AWS_REGION` | provided by Lambda runtime | Used for IAM token mint. |

---

## IAM scoping

The Lambda's execution role grants `rds-db:connect` to a single
`{aurora_cluster_resource_id, db_user}` tuple — no Secrets Manager
access. The DB user must have `rds_iam` membership plus the per-
table grants documented in `migrations/0006_rls_policies.sql`.

---

## Local development

```bash
cd customers/NPO/RSC/Allen_Institute/biodata-registry/services/registration-lambda
python -m pip install -e .[test]
# The shared Layer source lives at services/shared-layer/biodata_registry_shared
# — install it editable so tests can import it.
python -m pip install -e ../shared-layer
python -m pytest -q
```

The unit tests use `unittest.mock` plus a hand-rolled in-memory
Postgres double (`FakeConn`) so no real database is required:

- `aurora_connection` is monkey-patched to yield the fake connection.
- The fake records every executed SQL statement so tests assert on
  shape (`INSERT INTO data_asset (...)`, `INSERT INTO entity_revision
  (...)`) without committing anything.
- `data_asset_storage_uri_unique` collisions are simulated by raising
  a fake exception whose message contains the index name; the handler
  pattern-matches on the index name to surface
  `DUPLICATE_ENTITY` (matching real psycopg behavior).

The tests cover the eight scenarios from Task 16.1's deliverables:

1. `POST /assets` with valid body → 201 + revision row created.
2. `POST /assets` with duplicate storage_uri → 409 `DUPLICATE_ENTITY`.
3. `GET /assets/{id}` hidden by RLS → 404 `NOT_FOUND`.
4. `GET /assets/{id}` sensitive but caller is data_administrator → 200.
5. `GET /assets/{id}` sensitive but caller is viewer → 403
   `SENSITIVE_ACCESS_DENIED`.
6. `PUT /assets/{id}` → `revision_number` monotonically increments.
7. `POST /entities/subject` with valid body → 201.
8. `POST /entities/INVALID_TYPE` → 400 with structured error.

---

## Packaging

Terraform packages this directory plus the runtime deps from
`requirements.txt` (currently empty — everything rides the shared
Layer) into a deployment zip via the `lambdas/registration` module.
The OpenAPI spec is copied alongside `handler.py` so the in-Lambda
middleware can validate without an extra runtime fetch.

# Indexing Lambda — Allen BioData Registry PoC

The CDC consumer that fans Aurora WAL events out to DocumentDB and
OpenSearch. Sits behind the SQS FIFO queue provisioned by the
`cdc-pipeline` Terraform module (Task 17.1) and consumes the JSON
event shape produced by the CDC Reader Lambda
(`{op, schema, table, ts_ms, lsn, before, after, pk}`).

**Validates:** R1.7, R8.4, R17.9, R28.3, R28.4, R28.5, R28.6.

**Design references:**
- `design.md` §Components.12. Indexing_Lambda.
- `design.md` §Architecture.CDC Pipeline Architecture.
- `design.md` §Data Models.DocumentDB Document Shape.
- `design.md` §Data Models.OpenSearch Document Shape.

---

## What this Lambda does

1. **Filter** — drops events for tables we don't index (`app_user`,
   `entity_revision`, `lifecycle_transition`, etc.). The allow-list is
   the closed set of biological + governance entities the read stores
   need to surface.
2. **Hydrate** — JOINs the affected row back to Aurora with `space`
   and `organization` so every downstream document carries
   `space_id` / `org_id` / `is_sensitive`. For data assets it also
   fetches related shared entities (subject, instrument) via the
   `data_asset_subject` and `data_asset_instrument` junctions and
   asset-specific entities (session, acquisition, processing,
   quality_control, data_description) keyed on `data_asset_id`.
3. **Shape** — produces two parallel documents:
   - **DocumentDB** (MongoDB-compatible, aind-data-access-api-shaped)
     keyed by `_id = data_asset.id`. One collection per top-level
     entity type. Schema follows `design.md` §Data Models.DocumentDB
     Document Shape.
   - **OpenSearch** (denormalized, flattened) keyed by `id`. Per-field
     boosts (`species^3`, `instrument^2`, `name^2`) are configured at
     the index level, not in the document. Schema follows
     `design.md` §Data Models.OpenSearch Document Shape.
4. **Independent fan-out** — DocumentDB and OpenSearch writes each
   get their own try/except. A failure on one target does NOT block
   the other; failed events land in the DLQ tagged
   `target: "docdb" | "opensearch"` so operators can replay only
   the failed leg.
5. **Idempotency** — both writes are upsert-shaped:
   - DocumentDB: `replace_one({"_id": id}, doc, upsert=True)`.
   - OpenSearch: `index(id=id, body=doc)` (an `index` with an explicit
     `_id` is upsert by default).
6. **Delete events** — produce `delete_one` / `delete` calls. A 404
   on OpenSearch delete is treated as success (idempotency).
7. **Asset-child events** — events on `session`, `acquisition`,
   `processing`, `quality_control`, `data_description`, and the
   junction tables trigger a re-index of the parent `data_asset`
   so the denormalized arrays stay current.

## What this Lambda does NOT do

- **Never calls Bedrock.** OpenSearch documents are written with
  `embedding_pending: true` and a null `description_vec`. The
  `Embedding_Backfill_Lambda` (Task 19.2) populates vectors
  asynchronously on a 30-second EventBridge schedule. The grep test
  for "embedding" or "bedrock" in this handler should match only
  this comment.
- **Does not enforce RLS.** The indexer connects as a privileged
  Postgres role with `BYPASSRLS` so it sees every row regardless of
  governance scope. The `space_id`, `org_id`, and `is_sensitive`
  fields it writes to the read stores are how downstream consumers
  enforce access control. RLS is enforced at the *consumer* boundary
  (Search_Lambda for OpenSearch; aind-data-access-api client library
  for DocumentDB) — not at the indexer.

---

## The embedding-pending pattern

A core design decision: **embedding generation is split off from the
CDC critical path**. The reason is latency:

- Bedrock Knowledge Bases embedding API: 500–1500 ms p99 per call.
- CDC end-to-end latency budget: 5 seconds for DocumentDB +
  OpenSearch lexical visibility (R28.8).

If Indexing_Lambda waited for Bedrock on every CDC event, two failure
modes follow:

1. The 5-second budget gets eaten by a single embedding call.
2. A Bedrock outage halts the entire CDC pipeline — no DocumentDB
   updates either, even though DocumentDB doesn't need embeddings.

The pattern that solves both:

| Step | Owner | Latency target | Output |
|------|-------|----------------|--------|
| 1. Index lexical fields | Indexing_Lambda (this Lambda) | < 5s | OpenSearch doc with `embedding_pending: true`, `description_vec: null` |
| 2. Backfill embeddings | Embedding_Backfill_Lambda (Task 19.2) | every 30s | `description_vec` filled, `embedding_pending: false` |

During the up-to-30-second window between steps, hybrid semantic
search degrades to lexical-only on `embedding_pending: true`
documents. This is acceptable because lexical search is already
correct — semantic search just adds boost.

---

## Trust boundary

This Lambda runs as a **privileged service identity** that bypasses
RLS. It has no end-user identity context to forward — it processes
every WAL event regardless of who triggered the underlying write.

| Principal | DB user | RLS? | Why |
|-----------|---------|------|-----|
| Business Lambdas (Registration etc.) | per-Lambda DB user | Yes — `SET LOCAL app.current_user_id` | End-user requests must be filtered by visibility |
| Indexing_Lambda (this) | `cdc_indexer` | No — `BYPASSRLS` granted | Must see every row to produce visibility metadata |

The `cdc_indexer` role is created by the schema migration runner
(out of scope for this Lambda) with:

```sql
CREATE ROLE cdc_indexer LOGIN BYPASSRLS;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO cdc_indexer;
```

The IAM execution role's `rds-db:connect` policy is scoped to
exactly this DB user — no other principal can authenticate as
`cdc_indexer`.

---

## Environment variables

Injected by the `lambdas/indexing` Terraform module.

| Variable | Required | Purpose |
|---|---|---|
| `AURORA_HOST` | yes | Aurora writer endpoint hostname. |
| `AURORA_PORT` | no (default `5432`) | Aurora port. |
| `AURORA_DB` | no | Database name. Falls back to the `dbname` field of the Aurora secret. |
| `AURORA_SECRET_ARN` | yes | Secrets Manager ARN with `cdc_indexer` username/password (until IAM auth is wired). |
| `DOCDB_ENDPOINT` | yes | DocumentDB cluster endpoint hostname. |
| `DOCDB_PORT` | no (default `27017`) | DocumentDB port. |
| `DOCDB_SECRET_ARN` | yes | Secrets Manager ARN with cluster master credentials. |
| `DOCDB_CA_BUNDLE` | no (default `/opt/certs/global-bundle.pem`) | Path to the AWS RDS root CA bundle for TLS. |
| `OPENSEARCH_ENDPOINT` | yes | OpenSearch Serverless collection endpoint URL. |
| `OPENSEARCH_REGION` | no (defaults to `AWS_REGION`) | Region for SigV4 signing. |
| `DLQ_URL` | yes | URL of the FIFO DLQ where failed events land. |
| `DB_SSLMODE` | no (default `require`) | psycopg SSL mode for Aurora. |
| `DB_CONNECT_TIMEOUT_SECONDS` | no (default `10`) | TCP/TLS handshake timeout. |
| `LOG_LEVEL` | no (default `INFO`) | Python logging level. |

---

## DLQ payload shape

When a target write fails, the original CDC event is enqueued to the
DLQ with a small wrapper:

```json
{
  "target": "docdb",
  "error": "ServerSelectionTimeoutError: ...",
  "message_id": "<original SQS messageId>",
  "cdc_event": {
    "op": "U",
    "table": "data_asset",
    "lsn": "0/1A2B3C4D",
    "after": { "id": "...", ... }
  },
  "ts_ms": 1735689600000
}
```

`target` is `"docdb"`, `"opensearch"`, or `"indexer"` (the last covers
hydration / unparseable-event failures, before either target is
attempted). Operators replay by reading from the DLQ, filtering on
`target`, and re-publishing to the main queue.

---

## Local development

```bash
cd customers/NPO/RSC/Allen_Institute/biodata-registry/services/indexing-lambda

python -m pip install -e .[test]
python -m pytest -q
```

The unit tests mock every external dependency (psycopg, pymongo,
opensearch-py, boto3) so no AWS account or running database is
required. See `tests/test_handler.py` for the test matrix.

### LocalStack integration testing (deferred to Task 18.2)

Integration tests against LocalStack DocumentDB + OpenSearch live
under Task 18.2 (the CDC Eventual Consistency property test). Those
tests:

1. Start LocalStack with the DocumentDB + OpenSearch + SQS modules.
2. Apply a minimal Aurora schema via Docker Postgres.
3. Insert a row, watch it propagate via the CDC Reader, assert it
   appears in both read stores within 5s p99.

Run from the repository root:

```bash
docker compose -f docker-compose.localstack.yml up -d
python -m pytest tests/integration/test_cdc_consistency.py -v
```

---

## Packaging

Terraform packages this directory plus the runtime deps from
`requirements.txt` (psycopg, pymongo, opensearch-py, requests-aws4auth)
into a deployment zip via the `lambdas/indexing` module. The module's
packager runs `pip install --target` against a Linux x86_64 wheel
cache so the binary deps match the Lambda runtime.

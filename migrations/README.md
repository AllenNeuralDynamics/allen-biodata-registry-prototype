# Aurora Schema Migrations

Source-of-truth DDL for the Allen BioData Registry PoC. Aurora PostgreSQL
Serverless v2 is the only writer in the system; DocumentDB and OpenSearch are
populated downstream from Aurora's WAL via the CDC pipeline. Everything in
those read stores can be rebuilt by replaying these migrations and the seeder.

## Conventions

### Naming

Files are named:

```
NNNN_<short_slug>.sql
```

* `NNNN` — zero-padded four-digit ordinal, monotonically increasing. The
  migration runner (Task 8.1) applies files in **lexical order**, so the
  numeric prefix dictates execution order.
* `<short_slug>` — `snake_case` description of what the migration does.

Examples:

| Order | File                                   | Purpose                                  |
| ----- | -------------------------------------- | ---------------------------------------- |
| 1     | `0001_governance.sql`                  | Org / Space / User / role tables         |
| 2     | `0002_data_asset.sql`                  | Data_Asset + shared entity tables        |
| 3     | `0003_junctions.sql`                   | Many-to-many junction tables             |
| 4     | `0004_revisions_lifecycle_duplicates.sql` | Audit, lifecycle, duplicate flags     |
| 5     | `0005_collections_schemas.sql`         | Collections + schema registry            |
| 6     | `0006_rls_policies.sql`                | RLS policies + redacted views            |
| 7     | `0007_search_indexes.sql`              | ts_vector + pgvector search indexes      |

### Authoring rules

1. **Forward-only.** Every migration is purely additive or transformational.
   There are no `_down.sql` partners — rollback is handled by restoring from
   an Aurora snapshot, not by running a "down" script. This matches the
   PoC's deployment model.

2. **Idempotent.** Re-running a migration that has already succeeded must
   be a no-op:
   * Use `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`,
     `CREATE EXTENSION IF NOT EXISTS`.
   * Wrap `CREATE TYPE ... AS ENUM` in:
     ```sql
     DO $$
     BEGIN
       CREATE TYPE foo AS ENUM (...);
     EXCEPTION
       WHEN duplicate_object THEN NULL;
     END;
     $$;
     ```
   * For `ALTER` statements, use `IF EXISTS` / `IF NOT EXISTS` guards where
     PostgreSQL supports them; otherwise wrap in `DO $$ ... $$` with a
     pg_catalog probe.

3. **Comment everything.** Every table and every column gets `COMMENT ON`,
   referencing the requirement IDs (e.g. `R9.2`) and design-doc sections it
   implements. The comments end up in `pg_description` and are surfaced by
   `\d+` and the Bedrock Knowledge Base ingest, so they are part of the
   product surface area, not just internal docs.

4. **One migration, one concern.** A migration that creates governance
   tables does not also create RLS policies; that comes later in
   `0006_rls_policies.sql`. This keeps each step small enough to review and
   roll forward atomically.

5. **Reference design.md as authoritative.** The DDL in
   `.kiro/specs/allen-biodata-registry-poc/design.md` §Data Models.Aurora is
   the source of truth for column names, types, and FK semantics. Where a
   migration adds columns or constraints beyond what's in design.md, that
   addition is called out in the file header.

### Migration runner contract

The Python migration runner (Task 8.1) treats this directory as the corpus:

* It discovers every `*.sql` file in lexical order.
* It tracks applied versions in a `schema_version` table that the runner
  itself creates on first invocation.
* It applies each file inside a single transaction. Migrations that need
  to run outside a transaction (e.g. `CREATE INDEX CONCURRENTLY`) must
  declare so via a leading SQL comment (`-- +runner: no-transaction`); we
  have no such migrations today.
* Re-running `terraform apply` re-invokes the runner, which is a no-op
  once all migrations are recorded.

### Local linting

Before checking a migration in, run a syntax check. In order of preference:

1. **Local Postgres** — fastest feedback:
   ```bash
   psql -1 -f migrations/0001_governance.sql "$LOCAL_DB_URL"
   ```
2. **Docker Postgres** — when no local install:
   ```bash
   docker run --rm -v "$PWD/migrations:/m" -e POSTGRES_PASSWORD=x \
     postgres:16 sh -c 'pg_ctl start -D /var/lib/postgresql/data && \
     psql -U postgres -1 -f /m/0001_governance.sql'
   ```
3. **`pgsanity` / `sqlfluff`** — pure-Python static check (no DB needed):
   ```bash
   pipx run pgsanity migrations/0001_governance.sql
   ```

The migration runner will run these against the deployed Aurora cluster as
part of `terraform apply`; local linting is a sanity check only.

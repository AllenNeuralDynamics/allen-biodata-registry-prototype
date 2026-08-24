# Allen BioData Registry — KB Overview

This Knowledge Base provides Bedrock Claude with grounded context for
natural-language queries against the Allen Institute's biological data registry.

## What this KB contains

1. **Registry DDL** — `02_registry_ddl.sql`. The Postgres schema definitions for
   `data_asset`, `subject`, `instrument`, `session`, `acquisition`, `processing`,
   `quality_control`, `data_description`, governance tables (`organization`,
   `space`, `app_user`, `user_org_role`, `sharing_grant`), and supporting tables
   (`entity_revision`, `lifecycle_transition`, `duplicate_flag`, `collection`,
   `schema_definition`).

2. **JSONB field documentation** — `03_jsonb_fields.md`. Notes on the metadata
   JSONB columns: which keys are conventional, which are custom-schema-extended,
   and which are required for validation.

3. **Example NL→SQL queries** — `04_nl_sql_examples.json`. Curated pairs of
   natural-language questions and their parameterized SQL. The Search_Lambda's
   `POST /search/nl` path uses these as few-shot examples.

4. **Ontology mappings** — `05_ontology_taxonomy.json`. NCBI Taxonomy term
   mappings for the species values used in `subject.species` (mouse →
   `taxid:10090`, rat → `taxid:10116`, zebrafish → `taxid:7955`, etc.).

5. **Registry validation references** — `06_validation_sources.md`. Pointers to
   external validation sources (Addgene, NCBI GenBank, MGI) used by the
   Validation_Lambda for genomic / plasmid sanity checks.

## Trust boundary

The KB is read-only with respect to the registry. It contains documentation
about the schema — **never live data**. Live data is queried by the Search and
Registration Lambdas through API Gateway with full RLS enforcement; the KB only
helps Claude write the right SQL.

## Embedding model

This KB uses `amazon.titan-embed-text-v2:0` (1024-dim) for retrieval. The same
model is used by the Embedding_Backfill_Lambda to populate `description_vec` on
`data_asset` rows for the live search index.

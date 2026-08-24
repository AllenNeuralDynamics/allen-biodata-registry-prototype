# JSONB Field Conventions

The registry stores extended metadata in JSONB columns alongside the structured
relational columns. This document is the reference for which keys are
conventional and which are custom-schema-extended.

## `data_asset.metadata`

Common keys (always present when applicable):
- `modality` — same as `data_type` column when set; ECoG, MRI, behavior, etc.
- `acquisition_start_time` — ISO-8601 timestamp
- `acquisition_end_time` — ISO-8601 timestamp
- `lab_name` — origin lab
- `funding_source` — grant ID or program name
- `notes` — free-text scientific context
- `validation_errors` — populated by Validation_Lambda when status = invalid

Custom-schema extensions (vary by org):
- Any key not in the list above. The Custom_Schema attached via
  `data_asset.schema_id` defines required custom fields.

## `subject.metadata`

Common keys:
- `species_taxid` — NCBI Taxonomy ID (matches `species` text column)
- `strain` — e.g. C57BL/6J, BALB/c
- `genotype_alleles` — list of {gene, allele, zygosity}
- `weight_grams` — numeric
- `housing` — vivarium / lab housing notes

## `instrument.metadata`

Common keys:
- `firmware_version` — string
- `serial_number` — string
- `calibration_date` — ISO date
- `tip_dimensions` — for probes: {x_um, y_um, z_um}

## `entity_revision.metadata_snapshot`

The full JSON payload that was POSTed when the revision was created. Used for
point-in-time reconstruction of an entity's state at any prior revision.

## Querying tips

- Use Postgres JSONB operators: `metadata->>'key'` (text), `metadata->'key'`
  (jsonb), `@>` (containment), `?` (key exists).
- Indexes: GIN indexes on the metadata column accelerate containment queries.
- For nested keys: `metadata #>> '{path,to,key}'`.

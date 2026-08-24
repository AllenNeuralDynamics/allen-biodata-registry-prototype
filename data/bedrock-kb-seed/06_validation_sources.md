# External Validation Sources

The Validation_Lambda performs sanity checks on certain free-text fields by
referencing external authority files. This document describes the canonical
sources and the registry's caching strategy.

## Addgene (plasmids)

- Source: <https://www.addgene.org/>
- Used for: Validating plasmid identifiers in `subject.metadata.plasmids[]` and
  `procedures.metadata.injections[].plasmid_addgene_id`.
- Pattern: `addgene:{integer}` — the integer must match a known Addgene catalog
  number.
- PoC behavior: Validation is **format-only** (regex match). A nightly Lambda
  refreshes the Addgene mirror in a future iteration.

## NCBI GenBank (sequences)

- Source: <https://www.ncbi.nlm.nih.gov/genbank/>
- Used for: Validating accession numbers in genomic annotations.
- Pattern: `[A-Z]{1,2}\d{5,8}(\.\d+)?` — standard GenBank format.
- PoC behavior: Format-only validation.

## NCBI Taxonomy

- Source: <https://www.ncbi.nlm.nih.gov/taxonomy>
- Used for: Validating species values on `subject.species`. See
  `05_ontology_taxonomy.json` for the curated subset shipped with the registry.
- PoC behavior: The KB ships with a 12-species curated table; species not in
  the table are accepted but flagged in `validation_errors` as `species_not_in_curated_taxonomy`.

## MGI (Mouse Genome Informatics)

- Source: <http://www.informatics.jax.org/>
- Used for: Validating mouse strain and allele names in
  `subject.metadata.genotype_alleles[].mgi_id`.
- Pattern: `MGI:\d+`.
- PoC behavior: Format-only validation.

## When to extend

The following extensions are recommended once the PoC graduates to production:

1. Replace format-only validation with mirrored authority files (S3 + nightly
   refresh Lambda).
2. Add ontology mappings for instrument/probe types (RRID).
3. Add ontology mappings for brain regions (Allen Brain Atlas CCF).

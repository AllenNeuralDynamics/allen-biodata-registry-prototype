# `opensearch` Terraform module — Allen BioData Registry PoC

OpenSearch Serverless collection backing the Registry's discovery search
(R17, R28). Read by `Search_Lambda`, written by `Indexing_Lambda` (CDC
fan-out) and `Embedding_Backfill_Lambda` (async vector population).

**Validates:** R17.2, R17.3, R17.5, R17.6, R31.3, R32.2.

**Design references:** `design.md §Components.7. Search_Lambda`,
`design.md §Data Models.OpenSearch Document Shape`,
`design.md §Architecture.CDC Pipeline Architecture`.

---

## What this module provisions

| Resource | Purpose |
|---|---|
| `aws_kms_key` + `aws_kms_alias` | Customer-managed CMK + alias `alias/<name_prefix>-opensearch`. Key rotation enabled. (R31.3) |
| `aws_s3_bucket` (`<name_prefix>-opensearch-config-<account_id>`) | Stores the biodata synonyms file. KMS-encrypted, versioned, public access blocked. |
| `aws_s3_object` (`biodata_synonyms.txt`) | Uploaded copy of `templates/biodata_synonyms.txt`. (R17.3) |
| `aws_opensearchserverless_security_policy` (encryption) | KMS at rest with the CMK above. (R31.3) |
| `aws_opensearchserverless_security_policy` (network) | VPC-only; public access disabled for both collection and Dashboards. |
| `aws_opensearchserverless_vpc_endpoint` | Attached to the supplied private subnets and SG. The only ingress path. |
| `aws_opensearchserverless_collection` | Type `SEARCH`. Standby replicas configurable (default `ENABLED`). |
| `aws_opensearchserverless_access_policy` (data) | Grants principals in `var.principal_arns` index/document permissions. |

Collection name: `<name_prefix>-biodata`.

---

## What this module does NOT provision

**The indices themselves and their templates.** OpenSearch Serverless does
not expose index management through the Terraform AWS provider — there is
no `aws_opensearchserverless_index` resource. The data_asset / subject /
instrument indices are created post-deploy by either:

* a `null_resource` + `local-exec` running a small Python script that
  authenticates with SigV4 and `PUT`s each index using the templates
  under `templates/`, or
* a Python Lambda invoked by `aws_lambda_invocation` that does the same
  with the OpenSearch Python client.

This module exports `index_template_paths` and `synonyms_s3_uri` so the
post-apply step (Task 10's environment composition) can pick up the files
without hard-coding the module's filesystem layout. The JSON templates
themselves are checked into source control under `templates/` so they are
versioned alongside the rest of the module.

---

## Synonym file workflow (R17.3)

1. `templates/biodata_synonyms.txt` is the source of truth in the repo.
   Solr-format synonyms file, one rule per line, comma-separated terms
   expand bidirectionally. Lines starting with `#` are comments.
2. `terraform apply` uploads the file to S3 at
   `s3://<name_prefix>-opensearch-config-<account_id>/biodata_synonyms.txt`.
3. The post-apply bootstrap step downloads the file and either:
   * **inlines** its contents into each index template's synonym filter
     under `"synonyms"` (simpler, PoC default), or
   * uploads the file to the OpenSearch Serverless package store and
     points the synonym filter at `"synonyms_path"`.
4. **Updating the synonym list requires re-creating the affected
   indices** — OpenSearch reads synonym files at index-create time only.
   The bootstrap step is idempotent: re-running it skips indices that
   already exist. To pick up new synonyms in the PoC, drop and re-create
   the indices via the bootstrap step's `--force-recreate` flag (Task 10).

The PoC defaults are placeholders. The Allen Institute team will provide
the canonical synonym list once they review search behavior on real data.

---

## Index templates — field semantics

The three index templates encode the search behavior contracts from
`requirements.md` §17:

| Field | Type | Why |
|---|---|---|
| `id`, `space_id`, `org_id` | `keyword` | Exact-match and term-aggregation. `space_id` + `is_sensitive` are pushed down as filter clauses by `Search_Lambda` (R17.9, R8.4). |
| `name` | `text` (analyzer `biodata_text`) + `.raw` subfield (`keyword`) | BM25 match on the analyzer, exact-match on the subfield. Synonyms applied via the `biodata_synonyms` filter (R17.3). |
| `name_suggest` | `search_as_you_type` | Autocomplete (R17.6). `Search_Lambda`'s `GET /suggest` queries this field. |
| `description` | `text` (analyzer `biodata_text`) | BM25 + synonyms over long-form text. |
| `description_vec` | `knn_vector` dim=1024, HNSW + cosine similarity | Hybrid semantic search (R17.5). Dimension 1024 matches **Bedrock Titan Embeddings v2** default — keep them in sync if the Bedrock model changes. The Embedding_Backfill_Lambda populates these vectors on a 30s schedule; documents land with `embedding_pending: true` and a null vector first (`design.md §Architecture.CDC Pipeline Architecture`). |
| `species`, `sex`, `instrument`, `organization`, `modalities`, `lifecycle_state`, `validation_status` | `keyword` | Faceted aggregations (R17.4). |
| `metadata_flat` | `text` | Catch-all for arbitrary JSONB metadata flattened into a single field by Indexing_Lambda. Lets BM25 pick up any term that didn't make it into a structured field. |

The Subject and Instrument templates follow the same shape but are
simpler — they don't carry the lifecycle / validation / sensitive metadata
that the Data_Asset template does, since those concepts apply at the
asset level.

### Per-field boost configuration (R17.2)

Per-field boosts (`species^3`, `instrument^2`, `name^2`) are applied in
**`Search_Lambda`'s query DSL**, not in the index template. This is a
deliberate design choice (`design.md §Data Models.OpenSearch Document
Shape`): keeping boosts in the query layer lets us tune ranking without
re-creating indices. The index templates themselves declare field types
only — they do not pre-bake boosts.

---

## VPC-only access

The network policy denies public access for both the collection (data
plane) and Dashboards UI; the only ingress is the VPC endpoint
(`aws_opensearchserverless_vpc_endpoint`) attached to the private subnets
and the internal security group passed in via `var.security_group_ids`.

A reasonable production hardening: tighten the SG ingress rules to only
the source SGs of the Lambdas that actually call OpenSearch
(Indexing_Lambda, Search_Lambda, Embedding_Backfill_Lambda). For the PoC
the internal SG's "members of the SG can reach members of the SG" rule
is sufficient.

---

## Cost — PoC trade-off and decision point

OpenSearch Serverless has a minimum-OCU floor that drives a non-trivial
monthly bill even when there is no traffic. **This is the most expensive
component of the PoC stack.**

| Configuration | OCU floor | ~Monthly cost (us-west-2) |
|---|---|---|
| `standby_replicas = ENABLED` (default; multi-AZ, recommended) | ~4 OCUs (2 indexing + 2 search) | **~$700/mo** |
| `standby_replicas = DISABLED` (single-AZ, PoC cost-cutting) | ~2 OCUs | **~$350/mo** |

For comparison, **provisioned managed OpenSearch on `t3.small.search`**
runs ~$25/mo for a single-node cluster. **TODO — flag to customer at QC1**:
Serverless was chosen per the design (auto-scaling, zero-ops, native KNN),
but the PoC budget impact is significant. If the Allen Institute team
wants to optimize PoC cost, switching to managed OpenSearch (provisioned)
for the PoC and migrating to Serverless at production scale is a
defensible path:

* **Serverless (current default)**: zero-ops, auto-scales, native KNN, no
  node sizing decisions. Cost floor is the downside.
* **Managed (provisioned)**: ~28× cheaper at PoC scale, but: node sizing
  decisions, manual scaling, slightly different KNN plugin story, requires
  VPC + access-policy plumbing. Production-scale workload likely
  re-converges on Serverless anyway.

The decision should be made jointly with the customer at QC1. Either way,
`Search_Lambda`'s query layer is unchanged — both backends speak the
OpenSearch REST API. **Per `design.md`, the PoC follows Serverless** —
this module implements Serverless and exposes `standby_replicas` as the
single knob to halve cost (`DISABLED`).

### Other costs

* **KMS CMK**: <$1/mo (key + ~10k requests).
* **S3 (synonyms bucket)**: pennies — single small text file, KMS-encrypted.
* **VPC endpoint**: ~$7.30/mo per AZ + data charges. Small for the PoC.

Total module cost at default settings: roughly **$700–$720/mo**, dominated
by the Serverless OCU floor.

---

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| `name_prefix` | `string` | `"biodata-registry-dev"` | Resource name prefix. ≤24 chars (collection name is `<name_prefix>-biodata`, capped at 32). |
| `environment` | `string` | `"dev"` | Environment tag. |
| `project` | `string` | `"biodata-registry"` | Project tag. |
| `vpc_id` | `string` | _(required)_ | From `module.vpc.vpc_id`. |
| `private_subnet_ids` | `list(string)` | _(required, ≥1)_ | From `module.vpc.private_subnet_ids`. |
| `security_group_ids` | `list(string)` | _(required, ≥1)_ | Typically `[module.vpc.internal_security_group_id]`. |
| `principal_arns` | `list(string)` | `[]` | Lambda execution-role ARNs granted index/document permissions. Empty during initial provisioning is fine. |
| `standby_replicas` | `string` | `"ENABLED"` | `ENABLED` (HA, ~$700/mo) or `DISABLED` (~$350/mo). |
| `kms_deletion_window_in_days` | `number` | `7` | Pending-delete window for the KMS CMK (7–30). |
| `synonyms_bucket_force_destroy` | `bool` | `true` | If true, `terraform destroy` deletes the synonyms bucket even if it contains objects. |
| `tags` | `map(string)` | `{}` | Extra tags. |

## Outputs

| Name | Description |
|---|---|
| `collection_id` | Serverless collection ID. |
| `collection_arn` | Collection ARN — used in Lambda execution-role IAM policies. |
| `collection_name` | `<name_prefix>-biodata`. |
| `collection_endpoint` | `https://...` URL Lambdas issue queries against. |
| `dashboard_endpoint` | Dashboards UI URL (VPC-only). |
| `synonyms_bucket` | Name of the S3 bucket holding the synonyms file. |
| `synonyms_bucket_arn` | ARN of the synonyms bucket. |
| `synonyms_object_key` | `biodata_synonyms.txt`. |
| `synonyms_s3_uri` | `s3://<bucket>/<key>` for the uploaded synonyms file. |
| `kms_key_arn` | CMK ARN used for at-rest encryption. |
| `kms_key_alias` | `alias/<name_prefix>-opensearch`. |
| `vpc_endpoint_id` | OpenSearch Serverless VPC endpoint ID. |
| `index_names` | `["data_asset", "subject", "instrument"]`. |
| `index_template_paths` | Map of index name → on-disk template path. Consumed by the bootstrap step. |
| `synonyms_local_file_path` | On-disk path to the local copy of the synonyms file. |
| `encryption_policy_name`, `network_policy_name`, `data_access_policy_name` | Diagnostic. |

---

## Index template provisioning is deferred to a post-apply step

OpenSearch Serverless has no Terraform-native index resource. After
`terraform apply` succeeds, Task 10 runs a one-shot bootstrap step (a
`null_resource` + `local-exec` running Python with SigV4, OR a small
Python Lambda invoked via `aws_lambda_invocation`) that:

1. Reads `index_template_paths` to locate the three template JSON files.
2. Downloads `biodata_synonyms.txt` from `synonyms_s3_uri`.
3. For each template: inlines the synonym contents into the
   `biodata_synonyms` filter under `"synonyms"`, then `PUT`s the
   resulting body to `<collection_endpoint>/<index_name>` with SigV4
   auth.
4. Skips any index that already exists (idempotent).

The bootstrap step lives at `scripts/opensearch-bootstrap-indices.py`
(authored by Task 10).

---

## Example usage

In `terraform/envs/dev/main.tf`:

```hcl
module "vpc" {
  source = "../../modules/vpc"
  # ...
}

module "opensearch" {
  source = "../../modules/opensearch"

  name_prefix = "biodata-registry-dev"
  environment = "dev"
  project     = "biodata-registry"

  vpc_id             = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids
  security_group_ids = [module.vpc.internal_security_group_id]

  # Lambda execution-role ARNs are wired in once Tasks 18, 19, 28 author
  # those modules. Empty for the initial Phase 1 deploy.
  principal_arns = []

  # Halve the OCU floor for PoC cost-consciousness; flip to ENABLED for
  # production HA.
  standby_replicas = "DISABLED"

  tags = {
    Owner = "biodata-registry-team"
  }
}
```

---

## Validation

This module is consumed by the dev environment composition and is not
deployed standalone. To verify the module compiles:

```bash
cd customers/NPO/RSC/Allen_Institute/biodata-registry/terraform/modules/opensearch
terraform init -backend=false
terraform validate
terraform fmt -check
python3 -c "import json; json.load(open('templates/data_asset_index_template.json'))"
python3 -c "import json; json.load(open('templates/subject_index_template.json'))"
python3 -c "import json; json.load(open('templates/instrument_index_template.json'))"
```

`terraform plan` / `apply` are run in **Task 10** against the dev
environment composition.

---

## TODOs handed to downstream tasks

* **Task 10** — author `scripts/opensearch-bootstrap-indices.py` and
  wire it into the environment composition as a `null_resource`
  `local-exec` (or as a Python Lambda invoked via
  `aws_lambda_invocation`) so `terraform apply` covers index creation
  end-to-end.
* **Task 18.1 (Indexing_Lambda)** — add the Indexing_Lambda execution
  role ARN to `var.principal_arns`. Grant `kms:Decrypt` on
  `module.opensearch.kms_key_arn` and `s3:GetObject` on the synonyms
  bucket if the Lambda needs to re-read the synonym list at runtime.
* **Task 19.1 (Embedding_Backfill_Lambda)** — add the backfill Lambda's
  execution role ARN to `var.principal_arns`.
* **Task 28.1 (Search_Lambda)** — add the Search_Lambda execution role
  ARN to `var.principal_arns`. Apply per-field boosts (`species^3`,
  `instrument^2`, `name^2`) in the query DSL — they are intentionally
  NOT baked into the index templates (R17.2 design choice).
* **Customer review (QC1)** — confirm OpenSearch Serverless vs managed
  OpenSearch decision; replace placeholder synonym list with the
  Allen Institute canonical list.

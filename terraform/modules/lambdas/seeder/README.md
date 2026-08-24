# `lambdas/seeder` Terraform module

Packages, deploys, and IAM-scopes the Sample-Data Seeder Lambda that
streams a 10% sample of the aind-data-schema snapshot from S3 and
inserts the records into Aurora through the relational data-asset +
shared-entity graph defined by migrations 0001–0007.

**Validates:** R32.2 (sample data loaded), R32.5 (idempotent
`terraform apply`).

**Design references:**
- `design.md` §IaC.Idempotency and Sample Data
- `design.md` §Effort Estimation.Data Seeding
- `services/seeder/README.md` (Lambda-side documentation).

The Lambda source code lives at `services/seeder/`. This module:

1. Pip-installs the runtime deps (currently just `pg8000`) into a
   build directory.
2. Copies `handler.py`, `seeder.py`, `mapping.py` alongside.
3. Zips the result.
4. Provisions the Lambda function with VPC config and an IAM role
   scoped to:
   - `rds-db:connect` against one specific
     `{cluster_resource_id, db_user}` tuple, and
   - `s3:GetObject` against one specific `{bucket, key}` tuple.
5. Provisions an `aws_lambda_invocation` resource that invokes the
   Lambda synchronously on every `terraform apply` whose source-hash
   trigger has changed. A non-2xx response from the Lambda fails the
   apply.

---

## What this module provisions

| Resource | Purpose |
|---|---|
| `null_resource.package` | Pip-installs deps, copies `handler.py` + `seeder.py` + `mapping.py` into the build directory. Re-runs whenever the source-tree hash changes. |
| `data "archive_file" "package"` | Zips the build directory into the deployment package. |
| `aws_iam_role.exec` | Lambda execution role. |
| `aws_iam_role_policy_attachment.vpc` | Attaches `AWSLambdaVPCAccessExecutionRole` for ENI mgmt + CloudWatch Logs. |
| `aws_iam_role_policy.rds_db_connect` | Inline policy granting `rds-db:connect` to **one** `{cluster_resource_id, db_user}` tuple — Aurora IAM database authentication. |
| `aws_iam_role_policy.s3_get_object` | Inline policy granting `s3:GetObject` to **one** `{seed_s3_bucket, seed_s3_key}` tuple — the seed snapshot. |
| `aws_cloudwatch_log_group.this` | Explicit log group with retention (default 90 days). |
| `aws_lambda_function.this` | The Lambda itself, configured for Python 3.12, VPC-attached, and parameterised via env vars. |
| `aws_lambda_invocation.seed` | Synchronous invocation on every `terraform apply` (when triggered). Created only when `var.invoke_on_apply = true` (default). |

---

## IAM scoping & blast radius

The execution role's permissions are minimal and resource-scoped:

**Aurora connection** — `rds-db:connect` targets a single resource:

```
arn:aws:rds-db:<region>:<account>:dbuser:<aurora_cluster_resource_id>/<db_user>
```

The Lambda can connect *only* as the configured DB user
(`migration_runner` by default), *only* to the configured Aurora
cluster.

**S3 read** — `s3:GetObject` targets a single resource:

```
arn:aws:s3:::<seed_s3_bucket>/<seed_s3_key>
```

The seeder cannot list the bucket, write to it, or read any other
key. If a future iteration needs a different snapshot, update the
variable and re-apply — the IAM policy follows automatically.

**Blast radius caveat:** the `migration_runner` DB user is highly
privileged inside the database (`rds_superuser`). Compromise of an
IAM-auth token would let an attacker manipulate schema. Mitigations:

- 15-minute IAM-auth-token TTL.
- IAM scoping to a single cluster + single DB user.
- The Lambda runs in private subnets only.
- The Lambda is invoked by Terraform during apply, not by an
  external request path.

For the PoC this trade-off is acceptable. Production should split
the seeder role from a separate "INSERT-only" role with row-level
grants tied to the system space.

---

## Inputs

### Identity / tagging

| Name | Type | Default | Description |
|---|---|---|---|
| `name_prefix` | `string` | `"biodata-registry-dev"` | Prefix for every resource Name tag. |
| `environment` | `string` | `"dev"` | Environment tag. |
| `project` | `string` | `"biodata-registry"` | Project tag. |
| `tags` | `map(string)` | `{}` | Extra tags merged onto every resource. |

### Source / packaging

| Name | Type | Default | Description |
|---|---|---|---|
| `source_dir` | `string` | (required) | Absolute path to the Lambda source directory. From the dev composition, typically `"${path.module}/../../../../services/seeder"`. |
| `build_dir` | `string` | `null` | Override the default per-module staging directory. |
| `python_executable` | `string` | `"python3"` | Interpreter used for `pip install --target`. Should match the Lambda runtime (3.12). |

### Aurora connection (env vars)

| Name | Type | Default | Description |
|---|---|---|---|
| `db_host` | `string` | (required) | Aurora writer endpoint. From `module.aurora.cluster_endpoint`. |
| `db_port` | `number` | `5432` | Aurora port. |
| `db_name` | `string` | (required) | Database name. From `module.aurora.db_name`. |
| `db_user` | `string` | `"migration_runner"` | DB user the seeder authenticates as. |
| `aurora_cluster_resource_id` | `string` | (required) | Cluster resource id (`cluster-xxx`). From `module.aurora.cluster_resource_id`. Used to scope the IAM policy. |
| `aurora_cluster_arn` | `string` | `null` | Cluster ARN — informational. |

### Seed source (env vars)

| Name | Type | Default | Description |
|---|---|---|---|
| `seed_s3_bucket` | `string` | `"aind-scratch-data"` | S3 bucket of the snapshot. |
| `seed_s3_key` | `string` | `"jon.young/metadata_v2_records_20260324/data_assets.json"` | S3 key of the JSON snapshot. |
| `seed_sample_fraction` | `number` | `0.1` | Fraction in (0.0, 1.0] to seed. Deterministic per record content. |

### Networking

| Name | Type | Default | Description |
|---|---|---|---|
| `vpc_subnet_ids` | `list(string)` | (required) | Private subnet IDs that route to Aurora. From `module.vpc.private_subnet_ids`. |
| `vpc_security_group_ids` | `list(string)` | (required) | Security group IDs that can egress to Aurora on 5432 AND egress to S3. |

### Runtime / sizing

| Name | Type | Default | Description |
|---|---|---|---|
| `memory_mb` | `number` | `1024` | Lambda memory size. The seeder loads the entire snapshot file into memory; 1024 MB fits the 10% sample. |
| `timeout_seconds` | `number` | `900` | Lambda timeout. The 10% sample takes 5–15 minutes; default is the Lambda hard ceiling. |
| `log_retention_days` | `number` | `90` | CloudWatch Logs retention. |
| `log_level` | `string` | `"INFO"` | Python logging level. |
| `kms_key_arn` | `string` | `null` | Optional CMK for env-var encryption. |

### Invocation behavior

| Name | Type | Default | Description |
|---|---|---|---|
| `invoke_on_apply` | `bool` | `true` | When true, the Lambda is invoked synchronously on every `terraform apply` whose source-hash trigger has changed. |
| `invocation_payload` | `string` | `"{}"` | JSON payload passed to the Lambda. The handler accepts `{"bucket":...}`, `{"key":...}`, `{"sample_fraction":...}`, `{"max_records":...}` overrides. |

---

## Outputs

| Name | Description |
|---|---|
| `function_name` | Name of the Lambda. |
| `function_arn` | Lambda ARN. |
| `exec_role_arn` / `exec_role_name` | IAM role identifiers. |
| `log_group_name` / `log_group_arn` | CloudWatch Logs group identifiers. |
| `package_zip_path` | Path to the deployment zip. Diagnostics. |
| `source_hash` | SHA-256 of the source files driving package rebuilds. |
| `invocation_result` | JSON body returned by the seeder on the most recent invocation (the structured SeedSummary). |

---

## Example usage

In `terraform/envs/dev/main.tf`:

```hcl
module "vpc"               { source = "../../modules/vpc"               /* ... */ }
module "aurora"            { source = "../../modules/aurora"            /* ... */ }
module "migration_runner"  { source = "../../modules/lambdas/migration-runner" /* ... */ }

module "seeder" {
  source = "../../modules/lambdas/seeder"

  name_prefix = "biodata-registry-dev"
  environment = "dev"
  project     = "biodata-registry"

  source_dir = "${path.module}/../../../services/seeder"

  db_host                    = module.aurora.cluster_endpoint
  db_port                    = module.aurora.port
  db_name                    = module.aurora.db_name
  db_user                    = "migration_runner"
  aurora_cluster_resource_id = module.aurora.cluster_resource_id
  aurora_cluster_arn         = module.aurora.cluster_arn

  seed_s3_bucket       = "aind-scratch-data"
  seed_s3_key          = "jon.young/metadata_v2_records_20260324/data_assets.json"
  seed_sample_fraction = 0.1

  vpc_subnet_ids         = module.vpc.private_subnet_ids
  vpc_security_group_ids = [module.vpc.aurora_client_sg_id]

  # Sequencing: the seeder must run AFTER the migration runner has
  # applied the schema. This depends_on edge expresses the contract.
  depends_on = [module.migration_runner]

  tags = { Owner = "biodata-registry-team" }
}

output "seed_state" {
  value = module.seeder.invocation_result
}
```

---

## Validating the module

```bash
cd customers/NPO/RSC/Allen_Institute/biodata-registry/terraform/modules/lambdas/seeder
terraform init -backend=false
terraform validate
terraform fmt -check
```

`terraform plan` / `apply` are run against the dev environment
composition, not against this module directly.

---

## Out-of-band setup (handled by other tasks)

The module assumes the following are in place:

1. **Aurora cluster (Task 3.1)** — `iam_database_authentication_enabled
   = true`.
2. **`migration_runner` DB user** — created out-of-band, granted
   `rds_iam` membership and `rds_superuser`-equivalent privileges.
3. **Schema migrations applied (Task 8.1)** — the seeder's INSERTs
   require all 14 registry tables to exist with their UNIQUE
   constraints.
4. **S3 source snapshot** — the snapshot must already exist at the
   configured `{bucket, key}` tuple. The module does NOT manage the
   bucket.
5. **VPC routing (Task 2.1)** — the Lambda's subnets can reach Aurora
   on 5432 and S3 (via gateway VPC endpoint or NAT).

Until the source snapshot exists at the configured location, the
Lambda will deploy successfully but its first invocation will fail
with `NoSuchKey`.

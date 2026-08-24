# `lambdas/seed-smoke-test` Terraform module

Packages, deploys, and IAM-scopes the post-seed smoke test Lambda
that runs a fixed set of read-only SQL assertions against Aurora
*after* the seeder Lambda finishes, and **fails the Terraform apply**
when the seeded data is missing or relationally inconsistent.

**Validates:** R2.7 (FK constraints prevent orphan references), R32.5
(idempotent `terraform apply` — a successful apply guarantees seeded
data is present and consistent).

**Design references:**
- `design.md` §Testing Strategy.E2E Tests.QC1
- `design.md` §IaC.Idempotency and Sample Data
- `services/seed-smoke-test/README.md` (Lambda-side documentation).

The Lambda source code lives at `services/seed-smoke-test/`. This
module:

1. Pip-installs the runtime deps (currently just `pg8000`) into a
   build directory.
2. Copies `handler.py`, `smoke_test.py` alongside.
3. Zips the result.
4. Provisions the Lambda function with VPC config and an IAM role
   scoped to `rds-db:connect` against one specific
   `{cluster_resource_id, db_user}` tuple.
5. Provisions an `aws_lambda_invocation` resource that invokes the
   Lambda synchronously on every `terraform apply` whose source-hash
   trigger has changed. A non-2xx response from the Lambda fails the
   apply.

---

## What this module provisions

| Resource | Purpose |
|---|---|
| `null_resource.package` | Pip-installs deps, copies `handler.py` + `smoke_test.py` into the build directory. Re-runs whenever the source-tree hash changes. |
| `data "archive_file" "package"` | Zips the build directory into the deployment package. |
| `aws_iam_role.exec` | Lambda execution role. |
| `aws_iam_role_policy_attachment.vpc` | Attaches `AWSLambdaVPCAccessExecutionRole` for ENI mgmt + CloudWatch Logs. |
| `aws_iam_role_policy.rds_db_connect` | Inline policy granting `rds-db:connect` to **one** `{cluster_resource_id, db_user}` tuple — Aurora IAM database authentication. |
| `aws_cloudwatch_log_group.this` | Explicit log group with retention (default 90 days). |
| `aws_lambda_function.this` | The Lambda itself, configured for Python 3.12, VPC-attached, and parameterised via env vars. |
| `aws_lambda_invocation.verify` | Synchronous invocation on every `terraform apply` (when triggered). Created only when `var.invoke_on_apply = true` (default). |

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

There is **no S3 grant** — the smoke test reads exclusively from
Aurora; it has no reason to touch the seed snapshot.

**Blast radius caveat:** the `migration_runner` DB user is highly
privileged inside the database (`rds_superuser`, with `BYPASSRLS`).
Compromise of an IAM-auth token would let an attacker read every row
in the registry. Mitigations:

- 15-minute IAM-auth-token TTL.
- IAM scoping to a single cluster + single DB user.
- The Lambda runs in private subnets only.
- The Lambda is invoked by Terraform during apply, not by an
  external request path.

For the PoC this trade-off is acceptable. Production should split
to a dedicated read-only `smoke_test_runner` DB role with `SELECT`
grants on the registry tables only.

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
| `source_dir` | `string` | (required) | Absolute path to the Lambda source directory. From the dev composition, typically `"${path.module}/../../../services/seed-smoke-test"`. |
| `build_dir` | `string` | `null` | Override the default per-module staging directory. |
| `python_executable` | `string` | `"python3"` | Interpreter used for `pip install --target`. Should match the Lambda runtime (3.12). |

### Aurora connection (env vars)

| Name | Type | Default | Description |
|---|---|---|---|
| `db_host` | `string` | (required) | Aurora writer endpoint. From `module.aurora.cluster_endpoint`. |
| `db_port` | `number` | `5432` | Aurora port. |
| `db_name` | `string` | (required) | Database name. From `module.aurora.db_name`. |
| `db_user` | `string` | `"migration_runner"` | DB user the smoke test authenticates as. |
| `aurora_cluster_resource_id` | `string` | (required) | Cluster resource id (`cluster-xxx`). From `module.aurora.cluster_resource_id`. Used to scope the IAM policy. |
| `aurora_cluster_arn` | `string` | `null` | Cluster ARN — informational. |

### Smoke-test thresholds (env vars)

| Name | Type | Default | Description |
|---|---|---|---|
| `min_data_assets` | `number` | `10` | Minimum row count required in `data_asset`. |
| `min_subjects` | `number` | `1` | Minimum row count required in `subject`. |
| `min_instruments` | `number` | `1` | Minimum row count required in `instrument`. |
| `min_sessions` | `number` | `1` | Minimum row count required in `session`. |

### Networking

| Name | Type | Default | Description |
|---|---|---|---|
| `vpc_subnet_ids` | `list(string)` | (required) | Private subnet IDs that route to Aurora. From `module.vpc.private_subnet_ids`. |
| `vpc_security_group_ids` | `list(string)` | (required) | Security group IDs that can egress to Aurora on 5432. |

### Runtime / sizing

| Name | Type | Default | Description |
|---|---|---|---|
| `memory_mb` | `number` | `256` | Lambda memory size. The smoke test is tiny — every check is a single SELECT. |
| `timeout_seconds` | `number` | `60` | Lambda timeout. The whole suite finishes in under a second; 60s gives cold-start + IAM mint headroom. |
| `log_retention_days` | `number` | `90` | CloudWatch Logs retention. |
| `log_level` | `string` | `"INFO"` | Python logging level. |
| `kms_key_arn` | `string` | `null` | Optional CMK for env-var encryption. |

### Invocation behavior

| Name | Type | Default | Description |
|---|---|---|---|
| `invoke_on_apply` | `bool` | `true` | When true, the Lambda is invoked synchronously on every `terraform apply` whose source-hash trigger has changed. |
| `invocation_payload` | `string` | `"{}"` | JSON payload passed to the Lambda. The handler accepts `{"min_data_assets":...}`, `{"min_subjects":...}`, etc. overrides. |
| `invocation_extra_triggers` | `map(string)` | `{}` | Extra trigger key/value pairs that bump the smoke-test re-invocation hash. The dev composition typically passes `{ seeder_invocation = module.seeder.invocation_result }` so the smoke test re-runs every time the seeder produces a new summary. |

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
| `invocation_result` | JSON body returned by the smoke test on the most recent invocation (the structured SmokeSummary). A non-empty value here means every assertion passed (a failure would have failed the apply). |

---

## Example usage

In `terraform/envs/dev/main.tf`:

```hcl
module "vpc"               { source = "../../modules/vpc"               /* ... */ }
module "aurora"            { source = "../../modules/aurora"            /* ... */ }
module "migration_runner"  { source = "../../modules/lambdas/migration-runner" /* ... */ }
module "seeder"            { source = "../../modules/lambdas/seeder"            /* ... */ }

module "seed_smoke_test" {
  source = "../../modules/lambdas/seed-smoke-test"

  name_prefix = "biodata-registry-dev"
  environment = "dev"
  project     = "biodata-registry"

  source_dir = "${path.module}/../../../services/seed-smoke-test"

  db_host                    = module.aurora.cluster_endpoint
  db_port                    = module.aurora.port
  db_name                    = module.aurora.db_name
  db_user                    = "migration_runner"
  aurora_cluster_resource_id = module.aurora.cluster_resource_id
  aurora_cluster_arn         = module.aurora.cluster_arn

  # Production should bump these to a realistic floor (e.g.
  # min_data_assets ≈ 5000 against a 10% sample) so a partial-seed
  # failure is still caught.
  min_data_assets = 10
  min_subjects    = 1
  min_instruments = 1
  min_sessions    = 1

  vpc_subnet_ids         = module.vpc.private_subnet_ids
  vpc_security_group_ids = [module.vpc.aurora_client_sg_id]

  # Re-run the smoke test every time the seeder produces a new
  # summary. Without this trigger, a re-run of the seeder against
  # an existing seed would not re-verify Aurora.
  invocation_extra_triggers = {
    seeder_invocation = module.seeder.invocation_result
  }

  # Sequencing: the smoke test must run AFTER the seeder. This
  # depends_on edge expresses the contract.
  depends_on = [module.seeder]

  tags = { Owner = "biodata-registry-team" }
}

output "smoke_test_state" {
  value = module.seed_smoke_test.invocation_result
}
```

---

## Validating the module

```bash
cd customers/NPO/RSC/Allen_Institute/biodata-registry/terraform/modules/lambdas/seed-smoke-test
terraform init -backend=false
terraform validate
terraform fmt -check
```

`terraform plan` / `apply` are run against the dev environment
composition, not against this module directly.

---

## Out-of-band setup (handled by other tasks)

The module assumes the following are in place:

1. **Aurora cluster (Task 3.1)** — `iam_database_authentication_enabled = true`.
2. **`migration_runner` DB user** — created out-of-band.
3. **Schema migrations applied (Task 8.1)** — every assertion
   references tables created by 0001–0007.
4. **Seeder ran (Task 9.1)** — the smoke test verifies the seeder's
   output; running it before the seeder will fail the assertion on
   `min_data_assets`.
5. **VPC routing (Task 2.1)** — the Lambda's subnets can reach Aurora
   on 5432.

# `lambdas/migration-runner` Terraform module

Packages, deploys, and IAM-scopes the Schema Migration Runner Lambda
that applies the Aurora schema migrations under
`customers/.../biodata-registry/migrations/` and tracks applied
versions in a `schema_version` table.

**Validates:** R32.5 (idempotent `terraform apply`).

**Design references:**
- `design.md` §IaC.Idempotency and Sample Data.
- `migrations/README.md` (runner contract — filename convention, the
  `-- +runner: no-transaction` directive, forward-only convention).
- `services/migration-runner/README.md` (Lambda-side documentation).

The Lambda source code lives at `services/migration-runner/`. This
module:

1. Pip-installs the runtime deps (currently just `pg8000`) into a
   build directory.
2. Copies `handler.py` and `runner.py` alongside.
3. Copies every `*.sql` file from the migrations directory into
   `build/migrations/` so the Lambda finds them at
   `/var/task/migrations/` at runtime.
4. Zips the result.
5. Provisions the Lambda function with VPC config and an IAM role
   scoped to `rds-db:connect` against one specific
   `{cluster_resource_id, db_user}` tuple.
6. Provisions an `aws_lambda_invocation` resource that invokes the
   Lambda synchronously on every `terraform apply` whose source-hash
   trigger has changed. A non-2xx response from the Lambda fails the
   apply.

---

## What this module provisions

| Resource | Purpose |
|---|---|
| `null_resource.package` | Pip-installs deps, copies `handler.py` + `runner.py`, copies `migrations/*.sql` into the build directory. Re-runs whenever the source-tree hash changes. |
| `data "archive_file" "package"` | Zips the build directory into the deployment package. |
| `aws_iam_role.exec` | Lambda execution role. |
| `aws_iam_role_policy_attachment.vpc` | Attaches `AWSLambdaVPCAccessExecutionRole` for ENI mgmt + CloudWatch Logs. |
| `aws_iam_role_policy.rds_db_connect` | Inline policy granting `rds-db:connect` to **one** `{cluster_resource_id, db_user}` tuple — Aurora IAM database authentication. |
| `aws_cloudwatch_log_group.this` | Explicit log group with retention (default 90 days). |
| `aws_lambda_function.this` | The Lambda itself, configured for Python 3.12, VPC-attached, and parameterized via env vars. |
| `aws_lambda_invocation.migrate` | Synchronous invocation on every `terraform apply`. Created only when `var.invoke_on_apply = true` (default). |

---

## IAM scoping & blast radius

The execution role's `rds-db:connect` policy targets a single resource:

```
arn:aws:rds-db:<region>:<account>:dbuser:<aurora_cluster_resource_id>/<db_user>
```

This means the Lambda can connect *only* as the configured DB user
(typically `migration_runner`), *only* to the configured Aurora
cluster.

**Blast radius caveat:** the `migration_runner` DB user is itself
highly privileged inside the database (it must be able to
`CREATE EXTENSION`, `CREATE TABLE`, `GRANT`, `CREATE POLICY`,
`ALTER TABLE … ENABLE ROW LEVEL SECURITY` — see
`services/migration-runner/README.md` "Required DB user"). Compromise
of an IAM-auth token would let an attacker manipulate the schema.
This is mitigated by:

- 15-minute token TTL.
- IAM scoping to a single cluster + single DB user.
- The Lambda runs in private subnets only — no internet egress unless
  the VPC is configured to allow it.
- The Lambda is invoked by Terraform during apply, not by an external
  request path.

For the PoC this trade-off is acceptable. Production should consider
splitting the migration runner role from a separate "DDL only" role
that is granted the minimum subset of privileges each migration
actually needs, with the role rotated between applies.

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
| `source_dir` | `string` | (required) | Absolute path to the Lambda source directory. From the dev composition, typically `"${path.module}/../../../../services/migration-runner"`. |
| `migrations_dir` | `string` | (required) | Absolute path to the directory of `*.sql` files. From the dev composition, typically `"${path.module}/../../../../migrations"`. |
| `build_dir` | `string` | `null` | Override the default per-module staging directory. |
| `python_executable` | `string` | `"python3"` | Interpreter used for `pip install --target`. Should match the Lambda runtime (3.12). |

### Aurora connection (env vars)

| Name | Type | Default | Description |
|---|---|---|---|
| `db_host` | `string` | (required) | Aurora writer endpoint. From `module.aurora.cluster_endpoint`. |
| `db_port` | `number` | `5432` | Aurora port. |
| `db_name` | `string` | (required) | Database name. From `module.aurora.db_name`. |
| `db_user` | `string` | `"migration_runner"` | Privileged DB user the runner authenticates as. |
| `aurora_cluster_resource_id` | `string` | (required) | Cluster resource id (`cluster-xxx`). From `module.aurora.cluster_resource_id`. Used to scope the IAM policy. |
| `aurora_cluster_arn` | `string` | `null` | Cluster ARN — informational. |

### Networking

| Name | Type | Default | Description |
|---|---|---|---|
| `vpc_subnet_ids` | `list(string)` | (required) | Private subnet IDs that route to Aurora. From `module.vpc.private_subnet_ids`. |
| `vpc_security_group_ids` | `list(string)` | (required) | Security group IDs that can egress to Aurora on 5432. |

### Runtime / sizing

| Name | Type | Default | Description |
|---|---|---|---|
| `memory_mb` | `number` | `512` | Lambda memory size. |
| `timeout_seconds` | `number` | `300` | Lambda timeout. Generous default for first-run migrations. |
| `log_retention_days` | `number` | `90` | CloudWatch Logs retention. |
| `log_level` | `string` | `"INFO"` | Python logging level inside the Lambda. |
| `kms_key_arn` | `string` | `null` | Optional CMK for env-var encryption. |

### Invocation behavior

| Name | Type | Default | Description |
|---|---|---|---|
| `invoke_on_apply` | `bool` | `true` | When true, the Lambda is invoked synchronously on every `terraform apply` whose source-hash trigger has changed. |
| `invocation_payload` | `string` | `"{}"` | JSON payload passed to the Lambda. The handler accepts `{"applied_by": "..."}` or `{"migrations_dir": "..."}` overrides. |

---

## Outputs

| Name | Description |
|---|---|
| `function_name` | Name of the Lambda. |
| `function_arn` | Lambda ARN. |
| `exec_role_arn` / `exec_role_name` | IAM role identifiers. |
| `log_group_name` / `log_group_arn` | CloudWatch Logs group identifiers. |
| `package_zip_path` | Path to the deployment zip on the operator's machine. Diagnostics. |
| `source_hash` | SHA-256 of the source files + migrations corpus driving package rebuilds. |
| `invocation_result` | JSON body returned by the Lambda on the most recent invocation. |

---

## Example usage

In `terraform/envs/dev/main.tf`:

```hcl
module "vpc"     { source = "../../modules/vpc"     /* ... */ }
module "aurora"  { source = "../../modules/aurora"  /* ... */ }

module "migration_runner" {
  source = "../../modules/lambdas/migration-runner"

  name_prefix = "biodata-registry-dev"
  environment = "dev"
  project     = "biodata-registry"

  source_dir     = "${path.module}/../../../services/migration-runner"
  migrations_dir = "${path.module}/../../../migrations"

  db_host                    = module.aurora.cluster_endpoint
  db_port                    = module.aurora.port
  db_name                    = module.aurora.db_name
  db_user                    = "migration_runner"
  aurora_cluster_resource_id = module.aurora.cluster_resource_id
  aurora_cluster_arn         = module.aurora.cluster_arn

  vpc_subnet_ids         = module.vpc.private_subnet_ids
  vpc_security_group_ids = [module.vpc.aurora_client_sg_id]

  tags = { Owner = "biodata-registry-team" }
}

# Other modules that depend on the schema being applied should depend
# on this output so Terraform serializes them after migrations have run.
output "schema_state" {
  value = module.migration_runner.invocation_result
}
```

---

## Validating the module

```bash
cd customers/NPO/RSC/Allen_Institute/biodata-registry/terraform/modules/lambdas/migration-runner
terraform init -backend=false
terraform validate
terraform fmt -check
```

`terraform plan` / `apply` are run against the dev environment composition,
not against this module directly.

---

## Out-of-band setup (handled by other tasks)

The module assumes the following are in place:

1. **Aurora cluster (Task 3.1)** — `iam_database_authentication_enabled = true`,
   `vector` and `pg_trgm` extensions available, logical replication slot
   created.
2. **`migration_runner` DB user** — created out-of-band by the Aurora
   bootstrap (or manually as a one-off), granted `rds_iam` membership
   and `rds_superuser`-equivalent privileges within `biodata_registry`.
   The current Aurora module bootstrap runs as the master user; an
   additional bootstrap step is needed to create this user. See
   `services/migration-runner/README.md` "Required DB user" for the
   exact privilege set.
3. **VPC routing (Task 2.1)** — the Lambda's subnets can reach
   Aurora's security group on port 5432.

Until the `migration_runner` DB user is created, the Lambda will
deploy successfully but its first invocation will fail with
`authentication failed for user "migration_runner"`.

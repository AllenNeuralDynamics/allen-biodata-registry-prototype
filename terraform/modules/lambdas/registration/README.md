# `lambdas/registration` Terraform module

Packages, deploys, and IAM-scopes the Registration Lambda — the core
CRUD service for Data_Assets and Metadata_Entities behind API
Gateway. Mirrors the `lambdas/post-confirmation` and
`lambdas/authorizer` patterns: source-tree-hash drives package
rebuilds, IAM is scoped to a single Aurora cluster + DB user via
`rds-db:connect`, and the function runs in the VPC with private
subnets routing to the Aurora writer endpoint.

**Validates:** R1.1, R1.2, R1.4, R1.5, R1.6, R1.7, R2.4, R2.5, R2.6,
R6.1, R6.2, R28.2, R33.1, R33.2.

**Design references:**
- `design.md` §Components.2. Registration_Lambda.
- `design.md` §Architecture.RLS Enforcement Architecture.

The Lambda source code lives at
`services/registration-lambda/handler.py`. This module copies
`handler.py` + the OpenAPI spec into a build directory, zips the
result, and provisions the Lambda. Runtime deps (`psycopg`,
`aind-data-schema`, `openapi-core`, the shared `biodata_registry_shared`
package) come from the shared Layer (Task 12.1) — the deployment
zip itself is intentionally tiny (single-digit KB) so cold starts
stay fast.

---

## What this module provisions

| Resource | Purpose |
|---|---|
| `null_resource.package` | Copies `handler.py` + `openapi.yaml` into a build directory. Re-runs whenever the source-tree hash changes. |
| `data "archive_file" "package"` | Zips the build directory into the deployment package. |
| `aws_iam_role.exec` | Lambda execution role. |
| `aws_iam_role_policy_attachment.vpc` | Attaches `AWSLambdaVPCAccessExecutionRole` for ENI mgmt + CloudWatch Logs. |
| `aws_iam_role_policy.rds_db_connect` | Inline policy granting `rds-db:connect` to **one** `{cluster_resource_id, db_user}` tuple — Aurora IAM database authentication. |
| `aws_cloudwatch_log_group.this` | Explicit log group with retention (default 30 days). Avoids the implicit 'Never expire' group Lambda would otherwise create. |
| `aws_lambda_function.this` | The Lambda itself, configured for Python 3.12, VPC-attached, attached to the shared Layer, and parameterized via env vars. |

---

## IAM scoping

The execution role's `rds-db:connect` policy targets a single resource:

```
arn:aws:rds-db:<region>:<account>:dbuser:<aurora_cluster_resource_id>/<db_user>
```

This means the Lambda can connect *only* as the configured DB user,
*only* to the configured Aurora cluster. No Secrets Manager access, no
master-password path, no SSM permissions.

The DB user must have:
- `rds_iam` role membership (for IAM database authentication).
- `INSERT, UPDATE, SELECT` on `data_asset`, `subject`, `instrument`,
  `rig`, `procedures`, `session`, `acquisition`, `processing`,
  `quality_control`, `data_description`.
- `INSERT, SELECT` on `entity_revision` (`UPDATE`/`DELETE` are revoked
  from `PUBLIC` by migration 0004 — see design Property 3 on revision
  immutability).

These grants are configured by `migrations/0006_rls_policies.sql`.

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
| `source_dir` | `string` | (required) | Absolute path to the Lambda source directory. From the dev composition, typically `"${path.module}/../../../services/registration-lambda"`. |
| `openapi_spec_path` | `string` | derived | Override the OpenAPI spec path (defaults to `<source_dir>/../../openapi/openapi.yaml`). |
| `build_dir` | `string` | `null` | Override the default per-module staging directory. |
| `shared_layer_arn` | `string` | (required) | ARN of the shared Lambda Layer (Task 12.1). |

### Aurora connection (env vars)

| Name | Type | Default | Description |
|---|---|---|---|
| `aurora_host` | `string` | (required) | Aurora writer endpoint. From `module.aurora.cluster_endpoint`. |
| `aurora_port` | `number` | `5432` | Aurora port. |
| `aurora_db_name` | `string` | (required) | Database name. From `module.aurora.db_name`. |
| `db_user` | `string` | (required) | DB user the Lambda authenticates as. Must have `rds_iam` membership and INSERT/UPDATE/SELECT on the registry tables. |
| `aurora_cluster_resource_id` | `string` | (required) | Cluster resource id (`cluster-xxx`). From `module.aurora.cluster_resource_id`. Used to scope the IAM policy. |
| `aurora_cluster_arn` | `string` | `null` | Cluster ARN — informational. |
| `db_sslmode` | `string` | `"require"` | psycopg SSL mode. |
| `db_connect_timeout_seconds` | `number` | `10` | TCP/TLS handshake timeout. |
| `db_statement_timeout_ms` | `number` | `10000` | Per-statement timeout. |

### Networking

| Name | Type | Default | Description |
|---|---|---|---|
| `vpc_subnet_ids` | `list(string)` | (required) | Private subnet IDs that route to Aurora. From `module.vpc.private_subnet_ids`. |
| `vpc_security_group_ids` | `list(string)` | (required) | Security group IDs that can egress to Aurora on 5432. |

### Runtime / sizing

| Name | Type | Default | Description |
|---|---|---|---|
| `memory_mb` | `number` | `1024` | Lambda memory size. |
| `timeout_seconds` | `number` | `25` | Lambda timeout (5s buffer below API Gateway's 30s integration timeout). |
| `log_retention_days` | `number` | `30` | CloudWatch Logs retention. |
| `log_level` | `string` | `"INFO"` | Python logging level inside the Lambda. |
| `kms_key_arn` | `string` | `null` | Optional CMK for env-var encryption. |

---

## Outputs

| Name | Description |
|---|---|
| `function_name` | Name of the Lambda. |
| `function_arn` | Function ARN. |
| `invoke_arn` | **Wire this into the apigateway module via `var.registration_lambda_invoke_arn` for the AWS_PROXY integrations.** |
| `exec_role_arn` / `exec_role_name` | IAM role identifiers, useful for additional grants. |
| `log_group_name` / `log_group_arn` | CloudWatch Logs group identifiers. |
| `package_zip_path` | Path to the deployment zip on the operator's machine. Diagnostics. |
| `source_hash` | SHA-256 of the source files driving package rebuilds. |

---

## Example usage

In `terraform/envs/dev/main.tf`:

```hcl
module "vpc"           { source = "../../modules/vpc"           /* ... */ }
module "aurora"        { source = "../../modules/aurora"        /* ... */ }
module "shared_layer"  { source = "../../modules/lambda-layer"  /* ... */ }

module "registration_lambda" {
  source = "../../modules/lambdas/registration"

  name_prefix = "biodata-registry-dev"
  environment = "dev"
  project     = "biodata-registry"

  source_dir       = "${path.module}/../../../services/registration-lambda"
  shared_layer_arn = module.shared_layer.layer_arn

  aurora_host                = module.aurora.cluster_endpoint
  aurora_port                = module.aurora.port
  aurora_db_name             = module.aurora.db_name
  db_user                    = "registration_lambda"
  aurora_cluster_resource_id = module.aurora.cluster_resource_id
  aurora_cluster_arn         = module.aurora.cluster_arn

  vpc_subnet_ids         = module.vpc.private_subnet_ids
  vpc_security_group_ids = [module.vpc.internal_security_group_id]

  tags = { Owner = "biodata-registry-team" }
}

module "apigateway" {
  source = "../../modules/apigateway"

  # ... other inputs ...

  registration_lambda_invoke_arn        = module.registration_lambda.invoke_arn
  registration_lambda_function_name     = module.registration_lambda.function_name
}
```

---

## Validating the module

```bash
cd customers/NPO/RSC/Allen_Institute/biodata-registry/terraform/modules/lambdas/registration
terraform init -backend=false
terraform validate
terraform fmt -check
```

`terraform plan` / `apply` are run against the dev environment composition,
not against this module directly.

---

## Out-of-band setup (handled by other tasks)

The module assumes the following are in place:

1. **Aurora cluster (Task 3.1)** — `iam_database_authentication_enabled = true`.
2. **Schema migrations (Tasks 7.1–7.7)** — every registry table exists;
   the DB user passed via `var.db_user` exists, has `rds_iam` membership,
   and has the per-table grants documented in `migrations/0006_rls_policies.sql`.
3. **Shared Lambda Layer (Task 12.1)** — published; ARN passed via
   `var.shared_layer_arn`.
4. **OpenAPI spec (Task 13.1)** — `openapi/openapi.yaml` and the
   `openapi/components/schemas/*.json` files are checked in.
5. **VPC routing (Task 2.1)** — the Lambda's subnets can reach Aurora's
   security group on port 5432.

Until those land, the module's `terraform plan` will succeed but
`apply` will fail at the Lambda function provisioning step.

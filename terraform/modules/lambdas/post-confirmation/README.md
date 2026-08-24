# `lambdas/post-confirmation` Terraform module

Packages, deploys, and IAM-scopes the Cognito Post-Confirmation Lambda
that creates a bare `app_user` row in Aurora when a user finishes
confirming their account.

**Validates:** R19.3 (Post-Confirmation creates an `app_user` row in
Aurora).

**Design references:**
- `design.md` §Components.User Onboarding Flow.
- `design.md` §Components.Lambda Functions (the cognito module owns the
  trigger; this module owns the source code, IAM role, and packaging).

The Lambda source code lives at
`services/post-confirmation-lambda/handler.py`. This module pip-installs
the runtime deps from `requirements.txt`, copies `handler.py` alongside
them, zips the result, provisions the Lambda function, and exports
`function_arn` for the `cognito` module to wire as
`var.post_confirmation_lambda_arn`.

---

## What this module provisions

| Resource | Purpose |
|---|---|
| `null_resource.package` | Pip-installs deps + copies `handler.py` into a build directory. Re-runs whenever the source-tree hash changes. |
| `data "archive_file" "package"` | Zips the build directory into the deployment package. |
| `aws_iam_role.exec` | Lambda execution role. |
| `aws_iam_role_policy_attachment.vpc` | Attaches `AWSLambdaVPCAccessExecutionRole` for ENI mgmt + CloudWatch Logs. |
| `aws_iam_role_policy.rds_db_connect` | Inline policy granting `rds-db:connect` to **one** `{cluster_resource_id, db_user}` tuple — Aurora IAM database authentication. |
| `aws_cloudwatch_log_group.this` | Explicit log group with retention (default 30 days). Avoids the implicit 'Never expire' group Lambda would otherwise create. |
| `aws_lambda_function.this` | The Lambda itself, configured for Python 3.12, VPC-attached, and parameterized via env vars. |

---

## IAM scoping

The execution role's `rds-db:connect` policy targets a single resource:

```
arn:aws:rds-db:<region>:<account>:dbuser:<aurora_cluster_resource_id>/<db_user>
```

This means the Lambda can connect *only* as the configured DB user,
*only* to the configured Aurora cluster. No Secrets Manager access, no
master-password path, no SSM permissions. Adding rotation later is
purely a DB-side concern (rotating IAM auth tokens is automatic — they
are minted fresh on each invocation).

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
| `source_dir` | `string` | (required) | Absolute path to the Lambda source directory. From the dev composition, typically `"${path.module}/../../../services/post-confirmation-lambda"`. |
| `build_dir` | `string` | `null` | Override the default per-module staging directory. |
| `python_executable` | `string` | `"python3"` | Interpreter used for `pip install --target`. Should match the Lambda runtime (3.12). |

### Aurora connection (env vars)

| Name | Type | Default | Description |
|---|---|---|---|
| `db_host` | `string` | (required) | Aurora writer endpoint. From `module.aurora.cluster_endpoint`. |
| `db_port` | `number` | `5432` | Aurora port. |
| `db_name` | `string` | (required) | Database name. From `module.aurora.db_name`. |
| `db_user` | `string` | (required) | DB user the Lambda authenticates as. Must have `rds_iam` membership and INSERT on `app_user`. |
| `aurora_cluster_resource_id` | `string` | (required) | Cluster resource id (`cluster-xxx`). From `module.aurora.cluster_resource_id`. Used to scope the IAM policy. |
| `aurora_cluster_arn` | `string` | `null` | Cluster ARN — informational. |
| `app_user_has_org_id` | `bool` | `false` | Set to `true` once migration 7.1 adds an `org_id` column to `app_user`. |

### Networking

| Name | Type | Default | Description |
|---|---|---|---|
| `vpc_subnet_ids` | `list(string)` | (required) | Private subnet IDs that route to Aurora. From `module.vpc.private_subnet_ids`. |
| `vpc_security_group_ids` | `list(string)` | (required) | Security group IDs that can egress to Aurora on 5432. |

### Runtime / sizing

| Name | Type | Default | Description |
|---|---|---|---|
| `memory_mb` | `number` | `256` | Lambda memory size. |
| `timeout_seconds` | `number` | `10` | Lambda timeout — Cognito Post-Confirmation has a tight ~5s budget; 10 gives VPC cold-start headroom. |
| `log_retention_days` | `number` | `30` | CloudWatch Logs retention. |
| `log_level` | `string` | `"INFO"` | Python logging level inside the Lambda. |
| `kms_key_arn` | `string` | `null` | Optional CMK for env-var encryption. |

---

## Outputs

| Name | Description |
|---|---|
| `function_name` | Name of the Lambda. |
| `function_arn` | **Wire this into the cognito module via `var.post_confirmation_lambda_arn`.** |
| `invoke_arn` | API Gateway invoke ARN — exported for symmetry; Cognito does not use it. |
| `exec_role_arn` / `exec_role_name` | IAM role identifiers, useful for additional grants. |
| `log_group_name` / `log_group_arn` | CloudWatch Logs group identifiers. |
| `package_zip_path` | Path to the deployment zip on the operator's machine. Diagnostics. |
| `source_hash` | SHA-256 of the source files driving package rebuilds. |

---

## Example usage

In `terraform/envs/dev/main.tf`:

```hcl
module "vpc"     { source = "../../modules/vpc"     /* ... */ }
module "aurora"  { source = "../../modules/aurora"  /* ... */ }

module "post_confirmation_lambda" {
  source = "../../modules/lambdas/post-confirmation"

  name_prefix = "biodata-registry-dev"
  environment = "dev"
  project     = "biodata-registry"

  source_dir = "${path.module}/../../../services/post-confirmation-lambda"

  db_host                    = module.aurora.cluster_endpoint
  db_port                    = module.aurora.port
  db_name                    = module.aurora.db_name
  db_user                    = "post_confirmation_lambda"
  aurora_cluster_resource_id = module.aurora.cluster_resource_id
  aurora_cluster_arn         = module.aurora.cluster_arn

  vpc_subnet_ids         = module.vpc.private_subnet_ids
  vpc_security_group_ids = [module.vpc.aurora_client_sg_id]

  # Flip to true once migration 7.1 adds an org_id column to app_user.
  app_user_has_org_id = false

  tags = { Owner = "biodata-registry-team" }
}

module "cognito" {
  source = "../../modules/cognito"

  # ... other inputs ...

  post_confirmation_lambda_arn = module.post_confirmation_lambda.function_arn
}
```

---

## Validating the module

```bash
cd customers/NPO/RSC/Allen_Institute/biodata-registry/terraform/modules/lambdas/post-confirmation
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
2. **Schema migrations (Tasks 7.1–7.7)** — `app_user` table exists; the
   DB user passed via `var.db_user` exists, has `rds_iam` membership,
   and has `INSERT, SELECT` on `app_user`.
3. **VPC routing (Task 2.1)** — the Lambda's subnets can reach Aurora's
   security group on port 5432.

Until Task 7.1 / 8.1 land, the Lambda will deploy successfully but its
runtime invocation will fail with `relation "app_user" does not exist`
or `permission denied`. That is expected during the bring-up sequence
and is the reason the `cognito` module's Post-Confirmation wiring is
gated on `var.post_confirmation_lambda_arn` being non-null — so an
incomplete bring-up does not break sign-in.

# `lambdas/authorizer` Terraform module

Packages, deploys, and IAM-scopes the API Gateway custom authorizer
Lambda that fronts every authenticated endpoint of the BioData
Registry. The Lambda validates the Cognito JWT, resolves the auth
context (`{user_id, org_ids, space_ids, roles}`) from Aurora, and
returns the API Gateway IAM policy.

**Validates:** R9.7, R14.4, R19.4, R19.5.

**Design references:**
- `design.md` §Components.1. Authorizer_Lambda.
- `design.md` §Architecture.RLS Enforcement Architecture.

The Lambda source code lives at
`services/authorizer-lambda/handler.py`. This module pip-installs the
runtime deps from `requirements.txt`, copies `handler.py` alongside
them, zips the result, provisions the Lambda, and exports
`function_name` + `invoke_arn` for the `apigateway` module to wire as
the REQUEST authorizer.

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

The `aws_api_gateway_authorizer` + `aws_lambda_permission` resources
that wire this Lambda as the REQUEST authorizer live in the
`apigateway` module (Task 14.1) — passing `invoke_arn` and
`function_name` from this module's outputs is enough to enable them.

---

## IAM scoping

The execution role's `rds-db:connect` policy targets a single
resource:

```
arn:aws:rds-db:<region>:<account>:dbuser:<aurora_cluster_resource_id>/<db_user>
```

The Lambda can connect *only* as the configured DB user, *only* to
the configured Aurora cluster. No Secrets Manager access, no
master-password path, no SSM permissions.

The DB user must have `rds_iam` membership plus `SELECT` on the five
tables the handler queries: `app_user`, `user_org_role`,
`user_space_role`, `sharing_grant`, and `space`. Provisioning of the
DB user (with grants) is owned by the migration runner module
(Task 8.1).

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
| `source_dir` | `string` | (required) | Absolute path to the Lambda source directory. From the dev composition, typically `"${path.module}/../../../services/authorizer-lambda"`. |
| `build_dir` | `string` | `null` | Override the default per-module staging directory. |
| `python_executable` | `string` | `"python3"` | Interpreter used for `pip install --target`. Should match the Lambda runtime (3.12) — psycopg's manylinux wheel is loaded at install time so the CPython ABI must match. |
| `shared_layer_arn` | `string` | `null` | Optional shared Lambda Layer ARN. The Authorizer does not currently consume it (it produces the auth context rather than consuming it), so the default leaves the Layer detached — keeping the deployment package smaller and the cold start faster. |

### Cognito

| Name | Type | Default | Description |
|---|---|---|---|
| `cognito_user_pool_id` | `string` | (required) | User Pool ID — used for the JWT issuer URL + JWKS endpoint. From `module.cognito.user_pool_id`. |
| `cognito_app_client_id` | `string` | (required) | App Client ID — used as the JWT audience. From `module.cognito.user_pool_client_id`. |

### Aurora connection (env vars)

| Name | Type | Default | Description |
|---|---|---|---|
| `aurora_host` | `string` | (required) | Aurora writer endpoint. From `module.aurora.cluster_endpoint`. |
| `aurora_port` | `number` | `5432` | Aurora port. |
| `aurora_db_name` | `string` | (required) | Database name. From `module.aurora.db_name`. |
| `db_user` | `string` | (required) | DB user the Lambda authenticates as. Must have `rds_iam` membership + SELECT on the five tables listed above. |
| `aurora_cluster_resource_id` | `string` | (required) | Cluster resource id (`cluster-xxx`). From `module.aurora.cluster_resource_id`. Used to scope the IAM policy. |
| `aurora_cluster_arn` | `string` | `null` | Cluster ARN — informational. |
| `db_sslmode` | `string` | `"require"` | psycopg sslmode. |
| `db_connect_timeout_seconds` | `number` | `5` | TCP/TLS handshake timeout. Kept short — the Authorizer is on the hot path. |

### Networking

| Name | Type | Default | Description |
|---|---|---|---|
| `vpc_subnet_ids` | `list(string)` | (required) | Private subnet IDs that route to Aurora. From `module.vpc.private_subnet_ids`. |
| `vpc_security_group_ids` | `list(string)` | (required) | Security group IDs that can egress to Aurora on 5432 AND to Cognito's JWKS endpoint over HTTPS (the public IDP endpoint requires NAT-gateway egress from the private subnets). |

### Runtime / sizing

| Name | Type | Default | Description |
|---|---|---|---|
| `memory_mb` | `number` | `512` | Lambda memory size. |
| `timeout_seconds` | `number` | `10` | Lambda timeout — the Authorizer is on the hot path; 10 gives VPC cold-start headroom while keeping the timeout well under API Gateway's 30s integration ceiling. |
| `log_retention_days` | `number` | `30` | CloudWatch Logs retention. |
| `log_level` | `string` | `"INFO"` | Python logging level inside the Lambda. |
| `kms_key_arn` | `string` | `null` | Optional CMK for env-var encryption. |

---

## Outputs

| Name | Description |
|---|---|
| `function_name` | Plain function name. **Wire this into the apigateway module via `var.authorizer_lambda_function_name`.** |
| `function_arn` | Lambda function ARN. |
| `invoke_arn` | Invoke ARN. **Wire this into the apigateway module via `var.authorizer_lambda_arn`.** |
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
module "cognito" { source = "../../modules/cognito" /* ... */ }

module "authorizer_lambda" {
  source = "../../modules/lambdas/authorizer"

  name_prefix = "biodata-registry-dev"
  environment = "dev"
  project     = "biodata-registry"

  source_dir = "${path.module}/../../../services/authorizer-lambda"

  cognito_user_pool_id   = module.cognito.user_pool_id
  cognito_app_client_id  = module.cognito.user_pool_client_id

  aurora_host                = module.aurora.cluster_endpoint
  aurora_port                = module.aurora.port
  aurora_db_name             = module.aurora.db_name
  db_user                    = "authorizer_lambda"
  aurora_cluster_resource_id = module.aurora.cluster_resource_id
  aurora_cluster_arn         = module.aurora.cluster_arn

  vpc_subnet_ids         = module.vpc.private_subnet_ids
  vpc_security_group_ids = [module.vpc.aurora_client_sg_id]

  tags = { Owner = "biodata-registry-team" }
}

module "apigateway" {
  source = "../../modules/apigateway"

  # ... other inputs ...

  authorizer_lambda_arn           = module.authorizer_lambda.invoke_arn
  authorizer_lambda_function_name = module.authorizer_lambda.function_name
  cognito_user_pool_arn           = module.cognito.user_pool_arn
}
```

The apigateway module's `aws_api_gateway_authorizer.cognito` resource
sets `authorizer_result_ttl_in_seconds = 300` so a successful Allow
policy is reused for 5 minutes — Aurora is hit on cache miss only.

---

## Validating the module

```bash
cd customers/NPO/RSC/Allen_Institute/biodata-registry/terraform/modules/lambdas/authorizer
terraform init -backend=false
terraform validate
terraform fmt -check
```

`terraform plan` / `apply` are run against the dev environment
composition, not against this module directly.

---

## Out-of-band setup (handled by other tasks)

The module assumes the following are in place:

1. **VPC routing (Task 2.1)** — the Lambda's subnets can reach
   Aurora's security group on 5432 AND can egress to the public
   internet (NAT gateway) for Cognito's JWKS endpoint.
2. **Aurora cluster (Task 3.1)** —
   `iam_database_authentication_enabled = true`.
3. **Cognito user pool (Task 5.1)** — `module.cognito.user_pool_id`
   and `module.cognito.user_pool_client_id` exported.
4. **Schema migrations (Tasks 7.1–7.7)** — `app_user`,
   `user_org_role`, `user_space_role`, `sharing_grant`, `space`
   tables exist; the DB user passed via `var.db_user` exists, has
   `rds_iam` membership, and has `SELECT` on those five tables.
5. **API Gateway wiring (Task 14.1)** — the `apigateway` module
   consumes this module's outputs to attach the REQUEST authorizer.

Until those land, the Lambda will deploy successfully but its runtime
invocation will fail with `relation ... does not exist`,
`permission denied`, or a JWKS fetch timeout. That is expected during
the bring-up sequence — `var.authorizer_lambda_arn` on the
apigateway module is gated on a non-null value, so an incomplete
bring-up does not break public endpoints.

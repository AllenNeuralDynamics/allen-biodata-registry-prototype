# `apigateway` Terraform module — Allen BioData Registry PoC

Provisions the centralized REST API entry point for the Allen BioData
Registry: an API Gateway REST API whose definition is imported wholesale
from `openapi.yaml`, a deployed stage with CloudWatch logging and X-Ray
tracing, a default usage plan for per-key quota and throttling, stage-level
throttling for the per-IP leg of rate limiting, gateway responses that
inject `Retry-After` on 429 throttling and quota-exceeded paths, and a
conditional REQUEST authorizer wired to the Authorizer Lambda from
Task 15.1.

**Validates:**
- **R14.1** — REST API. Endpoint count comes from the OpenAPI spec; this module imports whatever the spec declares.
- **R14.2** — Per-key throttling via `aws_api_gateway_usage_plan.default` and per-IP throttling via stage-level `throttling_*_limit` in `aws_api_gateway_method_settings.all`.
- **R14.3** — `429 Too Many Requests` + `Retry-After` header via `aws_api_gateway_gateway_response.throttled` and `…quota_exceeded`.
- **R14.4** — Conditional REQUEST authorizer wired to the Authorizer Lambda. The OpenAPI spec is responsible for declaring which operations reference the authorizer.
- **R14.5** — `body = file(var.openapi_spec_path)` with `put_rest_api_mode = "overwrite"` — the spec is the single source of truth, and console hand-edits do not survive a re-apply.
- **R14.6** — Public/unauthenticated access to published-data endpoints is implemented in the OpenAPI spec by leaving `security: []` on those operations. This module does not enforce auth per-method (that belongs in the spec) — it only provides the authorizer to attach to non-public operations.

**Design references:**
- `design.md` §External Interfaces.API Gateway REST
- `design.md` §IaC.Terraform Modules (`apigateway`)

---

## OpenAPI spec contract (Task 13.1)

This module reads `openapi.yaml` at plan time and uploads its contents as
the API definition. The spec MUST satisfy **either** of the following so
that every operation actually routes to a backing Lambda:

1. **Inline integrations (preferred for the PoC):** every operation
   declares an `x-amazon-apigateway-integration` block specifying
   `httpMethod: POST`, `type: aws_proxy`, and `uri` pointing at the
   target Lambda's invoke ARN. This keeps all wiring in one file and is
   the path Task 13.1 takes.

2. **External integrations:** the spec omits integration blocks and the
   dev composition (terraform/envs/dev) layers
   `aws_api_gateway_integration` resources on top of this module's
   exported `api_gateway_id`. Use this path only if you need to compute
   integration URIs in HCL (e.g. when the Lambda module's outputs are
   not yet available at spec-author time).

If neither is in place, every call returns `500 Internal Server Error`
with `Execution failed due to configuration error: Invalid permissions
on Lambda function`.

### Marking endpoints public for unauthenticated access (R14.6)

The OpenAPI spec drives this. To mark an endpoint public:

```yaml
paths:
  /assets/{id}:
    get:
      summary: Get a published Data_Asset (no auth required)
      security: []        # explicit empty array → bypasses the global authorizer
      responses:
        '200':
          # ...
```

The global `security` declaration applies the authorizer everywhere by
default; per-operation `security: []` overrides that to allow
unauthenticated access. This is the standard OpenAPI 3.0 mechanism and
is honoured by API Gateway's spec import.

The Authorizer Lambda itself is responsible for ensuring that
unauthenticated callers can only see `lifecycle_state = 'published'`
data even on operations that DO require auth — but for endpoints that
serve only published data and want to skip the JWT round-trip entirely,
mark them public via `security: []`.

### Authorizer wiring in the spec

When `var.authorizer_lambda_arn` is non-null, this module creates
`aws_api_gateway_authorizer.cognito` of type `REQUEST`. The OpenAPI spec
should declare the corresponding `securityScheme`:

```yaml
components:
  securitySchemes:
    BiodataAuthorizer:
      type: apiKey
      name: Authorization
      in: header
      x-amazon-apigateway-authtype: custom
      x-amazon-apigateway-authorizer:
        type: request
        identitySource: method.request.header.Authorization
        authorizerCredentials: ""        # optional
        authorizerResultTtlInSeconds: 300

security:
  - BiodataAuthorizer: []
```

API Gateway's spec import maps the `x-amazon-apigateway-authorizer`
block to the same `aws_api_gateway_authorizer` resource by name. **No
manual ID substitution is required**, which is precisely why type =
REQUEST is referenced via the security-scheme name in the spec rather
than hardcoded against an authorizer ARN.

### Why REQUEST instead of COGNITO_USER_POOLS?

A pure `COGNITO_USER_POOLS` authorizer can only validate the JWT — it
cannot resolve the user's `org_ids`, `space_ids`, and `roles` from
Aurora, which the downstream business Lambdas need (R19.4 + R19.5 +
the three-layer RLS architecture in design.md §Architecture.RLS
Enforcement Architecture). A REQUEST (Lambda) authorizer can perform
the database lookup and return both the IAM allow/deny policy AND a
context object that API Gateway forwards to every backing Lambda.

`var.cognito_user_pool_arn` exists in the variables file for
documentation completeness — it would be needed by `provider_arns` of a
COGNITO_USER_POOLS authorizer — but the module does not consume it.
Leave it `null` for the PoC.

---

## Rate-limit behavior (R14.2 / R14.3)

Two layers of throttling, both with default values tuned for the PoC:

| Layer | Resource | Default | Purpose |
|---|---|---|---|
| **Per-API-key (R14.2 "per-key")** | `aws_api_gateway_usage_plan.default` | 5 rps + burst 10, 10 000 req/day quota | Bills back per consumer (Python_Client, MCP server). API key clients associate via `aws_api_gateway_usage_plan_key`. |
| **Stage / per-IP (R14.2 "per-IP")** | `aws_api_gateway_method_settings.all` | 10 rps + burst 20 | Caps every caller, keyed or not. Every operation on every method inherits this ceiling. |

When a client crosses either threshold:

* **THROTTLED** (per-second / per-minute throttle exceeded): API Gateway
  returns `429 Too Many Requests`. The
  `aws_api_gateway_gateway_response.throttled` resource overrides the
  default response shape to inject `Retry-After:
  <throttled_retry_after_seconds>` (default 30s) and a JSON body with
  `code: RATE_LIMIT_EXCEEDED`.
* **QUOTA_EXCEEDED** (daily request count exhausted): same 429 +
  Retry-After, but `code: QUOTA_EXCEEDED` so clients can distinguish
  short-term throttling from a flat-day-cap.

Tune via:

```hcl
module "apigateway" {
  # ...
  usage_plan_quota          = 100000
  usage_plan_throttle_burst = 100
  usage_plan_throttle_rate  = 50
  stage_throttle_burst      = 200
  stage_throttle_rate       = 100
  throttled_retry_after_seconds = 60
}
```

---

## Logging and tracing

* **Access logs:** structured JSON, one line per request, written to
  `/aws/apigateway/<name_prefix>-rest/access` with retention
  `var.log_retention_days` (default 14 days). Includes request ID,
  source IP, status, latency, authorizer principal ID, and integration
  error message.
* **Execution logs:** API Gateway's internal trace logs (auth decisions,
  request validation, integration responses), pushed to a CloudWatch
  group managed by API Gateway itself. Requires the account-level
  CloudWatch role (`aws_api_gateway_account.this`) — see the
  account-level resource caveat below.
* **Metrics:** detailed CloudWatch metrics enabled when
  `var.metrics_enabled = true` (default). Per-method 4xx/5xx counts,
  latency percentiles, and integration latency.
* **X-Ray tracing:** active tracing enabled on the stage when
  `var.enable_xray_tracing = true` (default). Useful for the PoC demo
  to debug end-to-end latency across API Gateway → Lambda → Aurora.

---

## Account-level resource caveat — `aws_api_gateway_account`

API Gateway requires an account+region-wide CloudWatch role to push
execution logs. This module owns the matching `aws_api_gateway_account`
resource so that a fresh `terraform apply` in a new AWS account works
end-to-end without manual setup.

**Trade-off:** there can only be ONE `aws_api_gateway_account` per AWS
account+region. If another module or Terraform configuration in the
same account-region also tries to manage this resource, the second
apply will overwrite the first. For the PoC this is fine because the
biodata-registry stack is the only API Gateway consumer in the
sandbox account; production composition should hoist this resource
out of the module into a top-level "account-bootstrap" config.

---

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| `name_prefix` | `string` | `"biodata-registry-dev"` | Resource-name prefix. |
| `environment` | `string` | `"dev"` | Stage name + env tag. |
| `project` | `string` | `"biodata-registry"` | Project tag. |
| `openapi_spec_path` | `string` | (required) | Absolute path to `openapi.yaml`. |
| `authorizer_lambda_arn` | `string` | `null` | Authorizer Lambda invoke ARN. `null` → no authorizer is created and every endpoint is unauthenticated until Task 15.1 lands. |
| `authorizer_lambda_function_name` | `string` | `null` | Plain function name of the Authorizer Lambda; required when `authorizer_lambda_arn` is non-null so the module can attach an `aws_lambda_permission`. |
| `cognito_user_pool_arn` | `string` | `null` | Documented for completeness; not consumed by this module (REQUEST authorizer is used instead of COGNITO_USER_POOLS). |
| `usage_plan_quota` | `number` | `10000` | Daily per-key quota. |
| `usage_plan_throttle_burst` | `number` | `10` | Per-key burst limit. |
| `usage_plan_throttle_rate` | `number` | `5` | Per-key steady-state rps. |
| `stage_throttle_burst` | `number` | `20` | Stage / per-IP burst limit. |
| `stage_throttle_rate` | `number` | `10` | Stage / per-IP steady-state rps. |
| `throttled_retry_after_seconds` | `number` | `30` | Value of the `Retry-After` header on 429 responses. |
| `enable_xray_tracing` | `bool` | `true` | Enable X-Ray active tracing on the stage. |
| `log_level` | `string` | `"INFO"` | One of `OFF`, `ERROR`, `INFO`. |
| `metrics_enabled` | `bool` | `true` | Enable detailed CloudWatch metrics. |
| `log_retention_days` | `number` | `14` | Access-log retention. |
| `endpoint_type` | `string` | `"REGIONAL"` | One of `REGIONAL`, `EDGE`, `PRIVATE`. |
| `tags` | `map(string)` | `{}` | Extra tags. |

## Outputs

| Name | Description |
|---|---|
| `api_gateway_id` | REST API ID. |
| `api_gateway_arn` | ARN of the REST API. |
| `api_gateway_execution_arn` | Execution ARN — append `/<stage>/<METHOD>/<resource>` (or `/*/*`) to scope an `aws_lambda_permission` source_arn for backing Lambdas. |
| `api_gateway_root_resource_id` | Root resource ID. |
| `api_gateway_invoke_url` | Public HTTPS invoke URL of the deployed stage. |
| `stage_name` | Stage name (= `var.environment`). |
| `stage_arn` | Stage ARN. |
| `usage_plan_id` | ID of the default usage plan. |
| `authorizer_id` | ID of the REQUEST authorizer, or `null` when no authorizer was wired. |
| `access_log_group_name` | Name of the CloudWatch Logs group receiving access logs. |
| `access_log_group_arn` | ARN of the access log group. |

---

## Cost

* **API Gateway REST:** $3.50 per million requests (us-west-2). The PoC's
  expected demo load (< 10 000 requests/day at the high end) is well
  inside the AWS Free Tier (1 M requests / 12 months).
* **CloudWatch Logs:** access-log volume scales with request count;
  default 14-day retention keeps ingest under the free tier for the PoC.
* **X-Ray:** $5 per million traces sampled. Default 5% sampling rate
  applied by API Gateway's active-tracing mode keeps cost negligible
  for a PoC; production should review the sampling rule.

---

## Example usage

In `terraform/envs/dev/main.tf`:

```hcl
locals {
  repo_root = "${path.module}/../../.."
}

module "lambdas" {
  source = "../../modules/lambdas"
  # ... outputs include the Authorizer Lambda's invoke_arn and function_name
}

module "apigateway" {
  source = "../../modules/apigateway"

  name_prefix       = "biodata-registry-dev"
  environment       = "dev"
  project           = "biodata-registry"
  openapi_spec_path = "${local.repo_root}/openapi.yaml"

  authorizer_lambda_arn           = module.lambdas.authorizer_invoke_arn
  authorizer_lambda_function_name = module.lambdas.authorizer_function_name

  # Defaults are fine for the PoC; tune for load testing.
  usage_plan_quota          = 10000
  usage_plan_throttle_burst = 10
  usage_plan_throttle_rate  = 5

  enable_xray_tracing = true

  tags = {
    Owner = "biodata-registry-team"
  }
}
```

To bootstrap **before** Task 15.1 lands (no authorizer Lambda yet), pass
`authorizer_lambda_arn = null` — the module will skip authorizer creation
and every endpoint becomes unauthenticated. The OpenAPI spec's
`security` blocks are still imported but have no effect until the
authorizer is wired in a subsequent apply.

---

## Validation

This module is consumed by the dev environment composition
(`terraform/envs/dev`) and is not deployed standalone. To verify the
module compiles cleanly:

```bash
cd customers/NPO/RSC/Allen_Institute/biodata-registry/terraform/modules/apigateway
terraform fmt -check
terraform init -backend=false
terraform validate
```

`terraform plan` / `apply` are run against the dev environment composition,
not against this module directly. **Note:** `terraform validate` will fail
unless `var.openapi_spec_path` resolves to an existing file at plan time
because `file()` is invoked in a `locals` block. Pass an existing path
(e.g. `openapi.yaml`) when validating the module standalone, or wrap the
validation in the dev composition where the path is computed from the
repo root.

---

## TODOs handed to Task 15.1 and the dev composition

* **Task 15.1:** Implement the Authorizer Lambda. Output its
  `invoke_arn` (the `arn:aws:apigateway:…:lambda:path/…` form, NOT the
  Lambda function ARN) and plain `function_name`.
* **Dev environment composition (Task 10):**
  - Wire `authorizer_lambda_arn = module.lambdas.authorizer_invoke_arn`
    and `authorizer_lambda_function_name = module.lambdas.authorizer_function_name`.
  - Mint API keys for the Python_Client and MCP server via
    `aws_api_gateway_api_key` and bind them to
    `module.apigateway.usage_plan_id` via `aws_api_gateway_usage_plan_key`.
  - Configure the CloudFront distribution (Task 6.1) to use
    `module.apigateway.api_gateway_invoke_url` as a behavior origin if the
    API is fronted by CloudFront for caching of public GET responses.
* **Production hardening (post-PoC):**
  - Hoist `aws_api_gateway_account` out of this module into a top-level
    account-bootstrap config so multiple API Gateway stacks can coexist.
  - Add a custom domain via `aws_api_gateway_domain_name` +
    `aws_api_gateway_base_path_mapping` so the API surface is
    `https://api.biodata-registry.alleninstitute.org/...`.
  - Tighten `Access-Control-Allow-Origin` on gateway responses (currently
    `*` for PoC convenience) to the production Web App origin.
  - Consider attaching AWS WAF to the stage for DDoS / SQLi protection
    (R31 explicitly excludes WAF from the PoC scope).

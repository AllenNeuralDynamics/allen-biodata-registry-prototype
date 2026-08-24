###############################################################################
# Variables — apigateway module
#
# Defaults are tuned for the Allen BioData Registry PoC (us-west-2, dev env).
# Every value can be overridden by the consuming environment composition.
###############################################################################

variable "name_prefix" {
  description = "Prefix applied to every resource name. Typically '<project>-<environment>', e.g. 'biodata-registry-dev'."
  type        = string
  default     = "biodata-registry-dev"

  validation {
    condition     = length(var.name_prefix) > 0 && length(var.name_prefix) <= 40
    error_message = "name_prefix must be 1–40 characters."
  }
}

variable "environment" {
  description = "Environment tag and stage name applied to the deployment (dev, staging, prod). The API Gateway stage_name is set to this value, so the invoke URL has the form 'https://<id>.execute-api.<region>.amazonaws.com/<environment>'."
  type        = string
  default     = "dev"

  validation {
    condition     = can(regex("^[a-zA-Z0-9_-]{1,30}$", var.environment))
    error_message = "environment must be 1–30 characters of letters, digits, underscore, or hyphen (API Gateway stage-name constraint)."
  }
}

variable "project" {
  description = "Project tag applied to every resource."
  type        = string
  default     = "biodata-registry"
}

variable "openapi_spec_path" {
  description = "Absolute path to the OpenAPI 3.0 spec file (typically '<repo>/openapi.yaml') that defines every REST endpoint. The module reads this file at plan time via 'file()' and imports it via aws_api_gateway_rest_api.body with mode = 'overwrite' — which means every apply REPLACES the full set of endpoints from the spec. Hand-edits in the AWS console will be wiped on the next apply (this is intentional: spec is source of truth, R14.5)."
  type        = string

  validation {
    condition     = length(var.openapi_spec_path) > 0
    error_message = "openapi_spec_path must be set to the absolute path of openapi.yaml."
  }
}

variable "authorizer_lambda_arn" {
  description = "Invoke ARN of the Authorizer Lambda (Task 15.1) that validates the Cognito JWT and resolves {user_id, org_ids, space_ids, roles} (R14.4, R19.4, R19.5). When null (PoC initial bootstrap, before Task 15.1 lands), the REQUEST authorizer is NOT created and every endpoint is unauthenticated — the dev composition wires the ARN here once the Lambda exists, and a subsequent terraform apply attaches the authorizer. The OpenAPI spec is responsible for declaring which endpoints reference the authorizer (via securitySchemes + security) and which are explicitly public (security: [])."
  type        = string
  default     = null

  validation {
    condition     = var.authorizer_lambda_arn == null || can(regex("^arn:aws[a-zA-Z-]*:apigateway:[a-z0-9-]+:lambda:path/", var.authorizer_lambda_arn))
    error_message = "authorizer_lambda_arn must be a Lambda invoke ARN of the form 'arn:aws:apigateway:<region>:lambda:path/2015-03-31/functions/arn:aws:lambda:<region>:<acct>:function:<name>/invocations', or null."
  }
}

variable "authorizer_lambda_function_name" {
  description = "Plain function name (not ARN) of the Authorizer Lambda. Used to attach an aws_lambda_permission so API Gateway is allowed to invoke the function. Required when authorizer_lambda_arn is non-null; ignored otherwise."
  type        = string
  default     = null
}

variable "cognito_user_pool_arn" {
  description = "Cognito User Pool ARN. Documented for completeness — the API Gateway REST_API authorizer type 'COGNITO_USER_POOLS' would need this in 'provider_arns', but this module uses a REQUEST authorizer (Lambda) instead, because the Authorizer Lambda must resolve org/space/role memberships from Aurora in addition to validating the JWT (R19.4 + R19.5). A pure Cognito authorizer cannot do the database lookup. This variable is therefore informational; leave null for the PoC."
  type        = string
  default     = null
}

variable "usage_plan_quota" {
  description = "Per-API-key daily quota for the default usage plan (R14.2). PoC default 10 000 requests/day — comfortably above any realistic interactive demo load and below the threshold where AWS bills meaningfully for this tier. Tune per environment when load characteristics are known."
  type        = number
  default     = 10000

  validation {
    condition     = var.usage_plan_quota > 0
    error_message = "usage_plan_quota must be a positive integer."
  }
}

variable "usage_plan_throttle_burst" {
  description = "Per-API-key burst limit for the default usage plan (max requests during a short spike). PoC default 10. Production should raise this to match expected concurrency."
  type        = number
  default     = 10

  validation {
    condition     = var.usage_plan_throttle_burst > 0
    error_message = "usage_plan_throttle_burst must be a positive integer."
  }
}

variable "usage_plan_throttle_rate" {
  description = "Per-API-key steady-state rate limit (requests/second) for the default usage plan. PoC default 5. Combined with the burst limit, this returns 429 + Retry-After when a client sustains > 5 req/s for long enough to drain the bucket (R14.2, R14.3)."
  type        = number
  default     = 5

  validation {
    condition     = var.usage_plan_throttle_rate > 0
    error_message = "usage_plan_throttle_rate must be a positive integer."
  }
}

variable "stage_throttle_burst" {
  description = "Per-IP / stage-level burst throttle (R14.2 'per-IP'). API Gateway exposes throttling at the stage level — every caller, keyed or not, is subject to this ceiling in addition to any usage-plan limit. PoC default 20."
  type        = number
  default     = 20

  validation {
    condition     = var.stage_throttle_burst > 0
    error_message = "stage_throttle_burst must be a positive integer."
  }
}

variable "stage_throttle_rate" {
  description = "Per-IP / stage-level steady-state rate (requests/second). PoC default 10."
  type        = number
  default     = 10

  validation {
    condition     = var.stage_throttle_rate > 0
    error_message = "stage_throttle_rate must be a positive integer."
  }
}

variable "throttled_retry_after_seconds" {
  description = "Value of the 'Retry-After' header returned with 429 responses (R14.3). Suggests the client wait this many seconds before retrying. PoC default 30 — generous enough to shed short bursts without forcing automation into long backoff cycles."
  type        = number
  default     = 30

  validation {
    condition     = var.throttled_retry_after_seconds > 0
    error_message = "throttled_retry_after_seconds must be a positive integer."
  }
}

variable "enable_xray_tracing" {
  description = "Enable AWS X-Ray active tracing on the stage. Default true for the PoC so we can debug end-to-end latency across API Gateway → Lambda → Aurora during demos. Set false to suppress X-Ray spend."
  type        = bool
  default     = true
}

variable "log_level" {
  description = "CloudWatch Logs level for API Gateway method execution logging. One of OFF, ERROR, INFO. PoC default INFO so request/response metadata is visible during demos; production should consider ERROR to reduce log volume."
  type        = string
  default     = "INFO"

  validation {
    condition     = contains(["OFF", "ERROR", "INFO"], var.log_level)
    error_message = "log_level must be one of OFF, ERROR, INFO."
  }
}

variable "metrics_enabled" {
  description = "Enable CloudWatch detailed metrics for the stage. Default true so per-method latency and 4xx/5xx counts are visible. Trade-off: enables paid CloudWatch metrics — fine for a PoC."
  type        = bool
  default     = true
}

variable "log_retention_days" {
  description = "Retention in days for the API Gateway access-log group. PoC default 14 days; production typically 90+."
  type        = number
  default     = 14

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1827, 3653], var.log_retention_days)
    error_message = "log_retention_days must be one of the values supported by CloudWatch Logs."
  }
}

variable "endpoint_type" {
  description = "API Gateway endpoint configuration: REGIONAL, EDGE, or PRIVATE. PoC default REGIONAL — the lowest-latency option for a single-region deployment, and the right choice when CloudFront sits in front of the API anyway."
  type        = string
  default     = "REGIONAL"

  validation {
    condition     = contains(["REGIONAL", "EDGE", "PRIVATE"], var.endpoint_type)
    error_message = "endpoint_type must be one of REGIONAL, EDGE, PRIVATE."
  }
}

variable "tags" {
  description = "Additional tags merged onto every resource. Project / Environment are added automatically."
  type        = map(string)
  default     = {}
}

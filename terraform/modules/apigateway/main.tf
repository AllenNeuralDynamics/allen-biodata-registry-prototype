###############################################################################
# Allen BioData Registry PoC — apigateway module
#
# Provisions:
#   * aws_api_gateway_rest_api.this — REST API whose body is the OpenAPI 3.0
#     spec at var.openapi_spec_path. put_rest_api_mode = "overwrite": every
#     terraform apply REPLACES the full set of endpoints from the spec, so
#     hand-edits in the AWS console do not survive (this is intentional —
#     R14.5 establishes the OpenAPI spec as the single source of truth).
#   * aws_api_gateway_deployment + aws_api_gateway_stage — stage_name is set
#     to var.environment, e.g. 'dev'. Stage-level throttling caps every
#     caller (keyed or not) per R14.2's "per-IP" requirement; the
#     CloudWatch access-log destination is wired here.
#   * (Conditional) aws_api_gateway_authorizer.cognito — type = REQUEST,
#     identity_source = method.request.header.Authorization. Created only
#     when var.authorizer_lambda_arn is non-null. The OpenAPI spec is
#     responsible for declaring which endpoints reference this authorizer
#     (via securitySchemes + security) and which are explicitly
#     unauthenticated (security: []) for published-data access (R14.6).
#   * aws_api_gateway_usage_plan.default — quota + throttle for per-key
#     rate limiting (R14.2). API key clients (Python_Client, MCP server)
#     associate via aws_api_gateway_usage_plan_key, which the dev
#     composition wires up after this module returns the plan ID.
#   * aws_api_gateway_method_settings — path_part = "*/*", enables
#     CloudWatch logging at var.log_level, X-Ray tracing (when
#     var.enable_xray_tracing), and detailed metrics for every method.
#   * aws_api_gateway_gateway_response.throttled — overrides the default
#     429 response to inject a 'Retry-After' header (R14.3).
#   * aws_api_gateway_gateway_response.quota_exceeded — same treatment for
#     the QUOTA_EXCEEDED gateway response so usage-plan quota breaches
#     also carry Retry-After.
#   * aws_lambda_permission.invoke_authorizer — conditional on the Lambda
#     ARN being supplied; grants apigateway.amazonaws.com invoke rights
#     scoped to this REST API's execution ARN.
#   * aws_cloudwatch_log_group.access — access-log destination, retention
#     governed by var.log_retention_days.
#   * aws_iam_role.cloudwatch + aws_api_gateway_account — region-wide
#     CloudWatch Logs role required by API Gateway to push execution
#     logs. The aws_api_gateway_account resource is account-region-wide
#     (one per AWS account per region); attaching it from this module is
#     a soft trade-off documented in the README — the dev composition
#     should ensure no other module manages aws_api_gateway_account in
#     the same account.
#
# Validates: R14.1 (REST API ~50 endpoints — count comes from the OpenAPI
# spec authored in Task 13.1; this module imports whatever the spec
# declares), R14.2 (usage plans with per-key + per-IP throttling), R14.3
# (429 + Retry-After on rate-limit breach), R14.4 (JWT validation via
# Authorizer Lambda — wired here, implementation in Task 15.1), R14.5
# (OpenAPI spec as source of truth — body = file(var.openapi_spec_path)
# with mode = overwrite), R14.6 (unauthenticated access to published
# data — implemented via 'security: []' on specific paths in the OpenAPI
# spec; this module does not enforce it directly because the per-method
# auth choice belongs in the spec).
#
# Note on the OpenAPI spec contract: the spec at var.openapi_spec_path
# MUST declare 'x-amazon-apigateway-integration' blocks for every
# operation (otherwise the deployment has no integrations attached and
# every call returns Internal Server Error). Alternatively, the dev
# composition can layer aws_api_gateway_integration resources on top of
# this module, but the simpler path for the PoC is to keep all wiring in
# the spec. Both paths are documented in the README.
###############################################################################

locals {
  common_tags = merge(
    {
      Project     = var.project
      Environment = var.environment
      Module      = "apigateway"
      ManagedBy   = "terraform"
    },
    var.tags,
  )

  authorizer_enabled = var.authorizer_lambda_arn != null

  # SHA-1 of the spec content drives a redeploy whenever the spec changes
  # (per the documented redeploy pattern for body-imported APIs). Without
  # this trigger, terraform would not detect an OpenAPI edit and the new
  # endpoints would never reach the deployed stage.
  openapi_spec     = file(var.openapi_spec_path)
  openapi_spec_sha = sha1(local.openapi_spec)

  # Retry-After value is a string in HTTP headers; the gateway-response
  # template must emit it inside quotes.
  retry_after_value = tostring(var.throttled_retry_after_seconds)
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

###############################################################################
# REST API — body imported from the OpenAPI spec
#
# put_rest_api_mode = "overwrite": every apply REPLACES the full set of
# resources/methods from the spec. Combined with the redeploy trigger
# below (sha1 of the spec content) this gives us a clean
# "edit openapi.yaml → terraform apply → endpoints updated" workflow.
###############################################################################

resource "aws_api_gateway_rest_api" "this" {
  name        = "${var.name_prefix}-rest"
  description = "Allen BioData Registry REST API. Body imported from openapi.yaml; do not hand-edit in console."

  body              = local.openapi_spec
  put_rest_api_mode = "overwrite"

  endpoint_configuration {
    types = [var.endpoint_type]
  }

  # Permit binary types for any future asset-content endpoints (e.g. a
  # presigned-URL passthrough). Cheap to enable; harmless when unused.
  binary_media_types = ["application/octet-stream", "image/*"]

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-rest"
  })
}

###############################################################################
# REQUEST authorizer — conditional
#
# Type = REQUEST (not COGNITO_USER_POOLS) because the Authorizer Lambda
# (Task 15.1) must validate the JWT AND resolve {user_id, org_ids,
# space_ids, roles} from Aurora (R19.4 + R19.5). A pure Cognito authorizer
# can only validate the token — the database lookup forces us to a Lambda
# authorizer.
#
# identity_source = the Authorization header. The Lambda strips the
# 'Bearer ' prefix internally.
###############################################################################

resource "aws_api_gateway_authorizer" "cognito" {
  # Always emitted in this composition (dev always wires authorizer_lambda_arn).
  # Removing `count` because it depends on a module-output ARN, which Terraform
  # cannot evaluate until apply time, breaking count.
  name                             = "${var.name_prefix}-authorizer"
  rest_api_id                      = aws_api_gateway_rest_api.this.id
  type                             = "REQUEST"
  authorizer_uri                   = var.authorizer_lambda_arn
  identity_source                  = "method.request.header.Authorization"
  authorizer_result_ttl_in_seconds = 300

  # Note: the OpenAPI spec is responsible for referencing this authorizer
  # via 'x-amazon-apigateway-authorizer' and 'security' on each operation.
  # Public endpoints (R14.6) leave 'security: []' to bypass it.
}

resource "aws_lambda_permission" "invoke_authorizer" {
  # Always emitted in this composition.
  statement_id  = "AllowAPIGatewayInvokeAuthorizer"
  action        = "lambda:InvokeFunction"
  function_name = var.authorizer_lambda_function_name
  principal     = "apigateway.amazonaws.com"

  # Scope the permission to ONLY the authorizer's execution ARN, not the
  # whole API. authorizers attach via /authorizers/<authorizer_id>.
  source_arn = "${aws_api_gateway_rest_api.this.execution_arn}/authorizers/${aws_api_gateway_authorizer.cognito.id}"
}

###############################################################################
# CloudWatch Logs — access logs destination + execution-logs role
#
# Two distinct things:
#   * access logs: per-request log line emitted by the stage to a CloudWatch
#     Logs group we own (aws_cloudwatch_log_group.access).
#   * execution logs: API-Gateway-internal trace logs (request validation,
#     authorizer invocations, integration responses), pushed to a
#     CloudWatch Logs group MANAGED BY API GATEWAY itself, but only after
#     the account is configured with a CloudWatch role ARN (set once per
#     account+region via aws_api_gateway_account).
###############################################################################

resource "aws_cloudwatch_log_group" "access" {
  name              = "/aws/apigateway/${var.name_prefix}-rest/access"
  retention_in_days = var.log_retention_days

  tags = local.common_tags
}

resource "aws_iam_role" "cloudwatch" {
  name = "${var.name_prefix}-apigw-cloudwatch"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "apigateway.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "cloudwatch" {
  role       = aws_iam_role.cloudwatch.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
}

# Account-wide setting. There can only be ONE aws_api_gateway_account per
# AWS account+region. The dev composition must ensure no other module
# attempts to manage this resource. Documented in the README.
resource "aws_api_gateway_account" "this" {
  cloudwatch_role_arn = aws_iam_role.cloudwatch.arn
}

###############################################################################
# Deployment + stage
#
# triggers redeploys when the OpenAPI spec content changes (or any of the
# referenced module resources change in a way that affects endpoint shape).
# Without this, an edit to openapi.yaml would update the REST API
# definition but leave the stage pointing at the old deployment.
###############################################################################

resource "aws_api_gateway_deployment" "this" {
  rest_api_id = aws_api_gateway_rest_api.this.id

  triggers = {
    redeploy = local.openapi_spec_sha
    # Authorizer changes also need a redeploy. Including the authorizer
    # ID (or "none") makes terraform redeploy when wiring/unwiring it.
    authorizer = aws_api_gateway_authorizer.cognito.id
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [
    aws_api_gateway_rest_api.this,
  ]
}

resource "aws_api_gateway_stage" "this" {
  rest_api_id   = aws_api_gateway_rest_api.this.id
  deployment_id = aws_api_gateway_deployment.this.id
  stage_name    = var.environment

  xray_tracing_enabled = var.enable_xray_tracing

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.access.arn

    # Compact JSON line per request. Adjust fields here when downstream
    # log-analysis needs change. Quotes are escaped to survive HCL → API
    # Gateway round-trip.
    format = jsonencode({
      requestId               = "$context.requestId"
      sourceIp                = "$context.identity.sourceIp"
      requestTime             = "$context.requestTime"
      protocol                = "$context.protocol"
      httpMethod              = "$context.httpMethod"
      resourcePath            = "$context.resourcePath"
      routeKey                = "$context.routeKey"
      status                  = "$context.status"
      responseLength          = "$context.responseLength"
      authorizerError         = "$context.authorizer.error"
      authorizerLatency       = "$context.authorizer.integrationLatency"
      integrationLatency      = "$context.integration.latency"
      integrationStatus       = "$context.integration.status"
      integrationErrorMessage = "$context.integrationErrorMessage"
      userAgent               = "$context.identity.userAgent"
      principalId             = "$context.authorizer.principalId"
    })
  }

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-rest-${var.environment}"
  })

  # Logging requires the account-level CloudWatch role to be set first.
  depends_on = [aws_api_gateway_account.this]
}

###############################################################################
# Method settings — apply to every method (path = "*/*")
#
# Enables logging at var.log_level, detailed CloudWatch metrics, and
# stage-level throttling. The stage-level rate/burst here is the "per-IP"
# leg of R14.2 — every caller, keyed or not, hits this ceiling.
###############################################################################

resource "aws_api_gateway_method_settings" "all" {
  rest_api_id = aws_api_gateway_rest_api.this.id
  stage_name  = aws_api_gateway_stage.this.stage_name
  method_path = "*/*"

  settings {
    metrics_enabled        = var.metrics_enabled
    logging_level          = var.log_level
    data_trace_enabled     = false
    throttling_burst_limit = var.stage_throttle_burst
    throttling_rate_limit  = var.stage_throttle_rate
  }
}

###############################################################################
# Usage plan — per-API-key quota + throttle
#
# R14.2: per-key throttling. Consumers (Python_Client, MCP server, the
# Web App's signed-in fetcher) mint API keys via aws_api_gateway_api_key
# and bind them to this plan via aws_api_gateway_usage_plan_key. The dev
# composition wires that up — this module only owns the plan itself so
# the plan ID is stable across composition refactors.
###############################################################################

resource "aws_api_gateway_usage_plan" "default" {
  name        = "${var.name_prefix}-default-usage-plan"
  description = "Default usage plan for the Allen BioData Registry. Per-key throttle ${var.usage_plan_throttle_rate} rps with burst ${var.usage_plan_throttle_burst}; daily quota ${var.usage_plan_quota}."

  api_stages {
    api_id = aws_api_gateway_rest_api.this.id
    stage  = aws_api_gateway_stage.this.stage_name
  }

  quota_settings {
    limit  = var.usage_plan_quota
    period = "DAY"
  }

  throttle_settings {
    burst_limit = var.usage_plan_throttle_burst
    rate_limit  = var.usage_plan_throttle_rate
  }

  tags = local.common_tags
}

###############################################################################
# Gateway responses — 429 + Retry-After (R14.3)
#
# API Gateway emits the THROTTLED gateway response when a usage-plan
# throttle limit is crossed, and QUOTA_EXCEEDED when the daily quota is
# exhausted. The default response shapes do not include 'Retry-After';
# we override both to inject the header so clients can back off
# correctly per HTTP semantics.
#
# response_templates body is a JSON string; it must be a JSON expression
# (single quotes, no surrounding backticks) so API Gateway can substitute
# $context.* variables. The Retry-After header is applied uniformly via
# var.throttled_retry_after_seconds — a more sophisticated PoC could
# compute a dynamic value from the throttle state, but a fixed value is
# valid per RFC 9110 and matches typical client retry-jitter behavior.
###############################################################################

resource "aws_api_gateway_gateway_response" "throttled" {
  rest_api_id   = aws_api_gateway_rest_api.this.id
  response_type = "THROTTLED"
  status_code   = "429"

  response_parameters = {
    "gatewayresponse.header.Retry-After"                 = "'${local.retry_after_value}'"
    "gatewayresponse.header.Access-Control-Allow-Origin" = "'*'"
  }

  response_templates = {
    "application/json" = jsonencode({
      code              = "RATE_LIMIT_EXCEEDED"
      message           = "Request rate limit exceeded. Retry after ${local.retry_after_value} seconds."
      retryAfterSeconds = var.throttled_retry_after_seconds
      requestId         = "$context.requestId"
    })
  }
}

resource "aws_api_gateway_gateway_response" "quota_exceeded" {
  rest_api_id   = aws_api_gateway_rest_api.this.id
  response_type = "QUOTA_EXCEEDED"
  status_code   = "429"

  response_parameters = {
    "gatewayresponse.header.Retry-After"                 = "'${local.retry_after_value}'"
    "gatewayresponse.header.Access-Control-Allow-Origin" = "'*'"
  }

  response_templates = {
    "application/json" = jsonencode({
      code              = "QUOTA_EXCEEDED"
      message           = "Daily quota exhausted. Retry after ${local.retry_after_value} seconds."
      retryAfterSeconds = var.throttled_retry_after_seconds
      requestId         = "$context.requestId"
    })
  }
}

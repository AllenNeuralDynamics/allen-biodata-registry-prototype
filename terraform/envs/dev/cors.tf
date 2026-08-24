###############################################################################
# CORS configuration for the API.
#
# The frontend at https://d24ttk5pwwl2vc.cloudfront.net (and the local Vite
# dev server) needs:
#
# 1. An OPTIONS method on every resource that the browser issues a preflight
#    against. We use MOCK integrations so the OPTIONS responses are returned
#    by API Gateway itself (no Lambda invocation, no auth requirement).
#
# 2. CORS headers on 4XX/5XX error responses. The browser surfaces these
#    even when the underlying request fails — without them, an authorizer
#    rejection looks like "Failed to fetch" rather than "401 Unauthorized".
#
# Allowed origins:
#   - http://localhost:5173      — local Vite dev server
#   - https://d24ttk5pwwl2vc.cloudfront.net — production CloudFront
#
# We use `*` for the preflight responses. Browsers don't allow `*` together
# with credentials, but we don't need cookie credentials — the SPA carries a
# Bearer JWT, which is fine with `*`.
###############################################################################

locals {
  # Every resource that needs CORS preflight support. The list is hand-
  # maintained alongside the API Gateway resources defined in main.tf.
  _cors_resources = {
    assets         = aws_api_gateway_resource.assets.id
    asset_id       = aws_api_gateway_resource.asset_id.id
    asset_publish  = aws_api_gateway_resource.asset_publish.id
    asset_register = aws_api_gateway_resource.asset_register.id
    asset_archive  = aws_api_gateway_resource.asset_archive.id
    asset_unpublish = aws_api_gateway_resource.asset_unpublish.id
    search         = aws_api_gateway_resource.search.id
    suggest        = aws_api_gateway_resource.suggest.id
    search_nl      = aws_api_gateway_resource.search_nl.id
    validate       = aws_api_gateway_resource.validate.id
    duplicates     = aws_api_gateway_resource.duplicates.id
    orgs           = aws_api_gateway_resource.orgs.id
    revisions      = aws_api_gateway_resource.revisions.id
    collections    = aws_api_gateway_resource.collections.id
    metrics        = aws_api_gateway_resource.metrics.id
    metrics_proxy  = aws_api_gateway_resource.metrics_proxy.id
    agent          = aws_api_gateway_resource.agent.id
    agent_chat     = aws_api_gateway_resource.agent_chat.id
    public_agent      = aws_api_gateway_resource.public_agent.id
    public_agent_chat = aws_api_gateway_resource.public_agent_chat.id
    mcp            = aws_api_gateway_resource.mcp.id
    mcp_tools      = aws_api_gateway_resource.mcp_tools.id
    mcp_invoke     = aws_api_gateway_resource.mcp_invoke.id
    healthz        = aws_api_gateway_resource.healthz.id
    public_stats   = aws_api_gateway_resource.public_stats.id
    public_assets  = aws_api_gateway_resource.public_assets.id
    public_asset_id = aws_api_gateway_resource.public_asset_id.id
  }
}

resource "aws_api_gateway_method" "cors_options" {
  for_each = local._cors_resources

  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = each.value
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "cors_options" {
  for_each = local._cors_resources

  rest_api_id = aws_api_gateway_rest_api.main.id
  resource_id = each.value
  http_method = aws_api_gateway_method.cors_options[each.key].http_method
  type        = "MOCK"

  request_templates = {
    "application/json" = "{\"statusCode\": 200}"
  }
}

resource "aws_api_gateway_method_response" "cors_options" {
  for_each = local._cors_resources

  rest_api_id = aws_api_gateway_rest_api.main.id
  resource_id = each.value
  http_method = aws_api_gateway_method.cors_options[each.key].http_method
  status_code = "200"

  response_models = {
    "application/json" = "Empty"
  }

  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Headers" = true
    "method.response.header.Access-Control-Max-Age"       = true
  }
}

resource "aws_api_gateway_integration_response" "cors_options" {
  for_each = local._cors_resources

  rest_api_id = aws_api_gateway_rest_api.main.id
  resource_id = each.value
  http_method = aws_api_gateway_method.cors_options[each.key].http_method
  status_code = aws_api_gateway_method_response.cors_options[each.key].status_code

  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = "'*'"
    "method.response.header.Access-Control-Allow-Methods" = "'GET,POST,PUT,DELETE,OPTIONS'"
    "method.response.header.Access-Control-Allow-Headers" = "'Authorization,Content-Type,X-Amz-Date,X-Api-Key,X-Amz-Security-Token,X-Agent-Source,X-API-Source'"
    "method.response.header.Access-Control-Max-Age"       = "'600'"
  }

  depends_on = [aws_api_gateway_integration.cors_options]
}

###############################################################################
# Gateway responses — apply CORS headers to 4XX/5XX responses generated by
# API Gateway itself (e.g. authorizer rejections). Without these, the
# browser sees a CORS error rather than the actual HTTP status.
###############################################################################

resource "aws_api_gateway_gateway_response" "default_4xx" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  response_type = "DEFAULT_4XX"

  response_parameters = {
    "gatewayresponse.header.Access-Control-Allow-Origin"  = "'*'"
    "gatewayresponse.header.Access-Control-Allow-Headers" = "'Authorization,Content-Type'"
  }
}

resource "aws_api_gateway_gateway_response" "default_5xx" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  response_type = "DEFAULT_5XX"

  response_parameters = {
    "gatewayresponse.header.Access-Control-Allow-Origin"  = "'*'"
    "gatewayresponse.header.Access-Control-Allow-Headers" = "'Authorization,Content-Type'"
  }
}

resource "aws_api_gateway_gateway_response" "unauthorized" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  response_type = "UNAUTHORIZED"
  status_code   = "401"

  response_parameters = {
    "gatewayresponse.header.Access-Control-Allow-Origin"  = "'*'"
    "gatewayresponse.header.Access-Control-Allow-Headers" = "'Authorization,Content-Type'"
  }
}

resource "aws_api_gateway_gateway_response" "missing_auth" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  response_type = "MISSING_AUTHENTICATION_TOKEN"
  status_code   = "403"

  response_parameters = {
    "gatewayresponse.header.Access-Control-Allow-Origin"  = "'*'"
    "gatewayresponse.header.Access-Control-Allow-Headers" = "'Authorization,Content-Type'"
  }
}

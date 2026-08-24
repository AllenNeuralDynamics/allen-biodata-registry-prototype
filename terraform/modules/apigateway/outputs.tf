###############################################################################
# Outputs — apigateway module
#
# These outputs are the public contract consumed by the lambdas module
# (resource policy source_arn for invoke permissions), the cloudfront-s3
# module (origin domain), and any test harness needing the invoke URL.
# Renaming or removing any of them is a breaking change.
###############################################################################

output "api_gateway_id" {
  description = "ID of the API Gateway REST API. Consumed when constructing aws_lambda_permission source_arn for backing Lambdas, and by the dev composition when wiring custom domain / API mappings."
  value       = aws_api_gateway_rest_api.this.id
}

output "api_gateway_arn" {
  description = "ARN of the REST API itself (NOT the execution ARN). Useful for IAM resource references."
  value       = aws_api_gateway_rest_api.this.arn
}

output "api_gateway_execution_arn" {
  description = "Execution ARN of the REST API in the form 'arn:aws:execute-api:<region>:<acct>:<api_id>'. Append '/<stage>/<METHOD>/<resource_path>' (or '/*/*' for everything) to scope an aws_lambda_permission source_arn so backing Lambdas only accept invocations from this API."
  value       = aws_api_gateway_rest_api.this.execution_arn
}

output "api_gateway_root_resource_id" {
  description = "Root resource ID of the REST API. Rarely needed when the API is built from an OpenAPI body, but exported for parity with the standard module pattern and for any out-of-band aws_api_gateway_resource additions a downstream module might want to layer on top."
  value       = aws_api_gateway_rest_api.this.root_resource_id
}

output "api_gateway_invoke_url" {
  description = "Public HTTPS invoke URL of the deployed stage, e.g. 'https://abc123.execute-api.us-west-2.amazonaws.com/dev'. Consumed by the React Web App, the Python_Client, and the MCP Server."
  value       = aws_api_gateway_stage.this.invoke_url
}

output "stage_name" {
  description = "Name of the deployed API Gateway stage (set to var.environment, e.g. 'dev')."
  value       = aws_api_gateway_stage.this.stage_name
}

output "stage_arn" {
  description = "ARN of the deployed stage. Used for X-Ray sampling rules and CloudWatch alarm targets."
  value       = aws_api_gateway_stage.this.arn
}

output "usage_plan_id" {
  description = "ID of the default usage plan with quota + throttle (R14.2). Consumers of the API mint API keys via aws_api_gateway_api_key and associate them with this plan via aws_api_gateway_usage_plan_key."
  value       = aws_api_gateway_usage_plan.default.id
}

output "authorizer_id" {
  description = "ID of the REQUEST authorizer wired to the Authorizer Lambda, or null when var.authorizer_lambda_arn was null at apply time. The OpenAPI spec references this via x-amazon-apigateway-authorizer; this output exists for diagnostics and for any out-of-band integrations that need to attach the same authorizer."
  value       = aws_api_gateway_authorizer.cognito.id
}

output "access_log_group_name" {
  description = "Name of the CloudWatch Logs group receiving API Gateway access logs. Useful for downstream log subscription or metric filters."
  value       = aws_cloudwatch_log_group.access.name
}

output "access_log_group_arn" {
  description = "ARN of the CloudWatch Logs group receiving API Gateway access logs."
  value       = aws_cloudwatch_log_group.access.arn
}

###############################################################################
# Outputs — lambdas/authorizer module
#
# These outputs are the public contract consumed by the apigateway
# module (REQUEST authorizer wiring + invoke permission) and the dev
# composition (debugging convenience). Renaming or removing any of
# them is a breaking change.
###############################################################################

output "function_name" {
  description = "Plain function name (not ARN) of the Authorizer Lambda. Pass this to the apigateway module's `var.authorizer_lambda_function_name` so it can attach an aws_lambda_permission scoped to the API's authorizer ARN."
  value       = aws_lambda_function.this.function_name
}

output "function_arn" {
  description = "ARN of the Authorizer Lambda function. Note: API Gateway's authorizer_uri requires the *invoke* ARN, not this; pass `invoke_arn` to the apigateway module instead."
  value       = aws_lambda_function.this.arn
}

output "invoke_arn" {
  description = "Lambda invoke ARN — the form 'arn:aws:apigateway:<region>:lambda:path/2015-03-31/functions/<function_arn>/invocations'. Pass this to the apigateway module's `var.authorizer_lambda_arn`."
  value       = aws_lambda_function.this.invoke_arn
}

output "exec_role_arn" {
  description = "ARN of the Lambda's IAM execution role. Useful when granting additional permissions (e.g. KMS Decrypt on a custom env-var CMK)."
  value       = aws_iam_role.exec.arn
}

output "exec_role_name" {
  description = "Name of the Lambda's IAM execution role. Symmetric with exec_role_arn for callers that prefer name-based references."
  value       = aws_iam_role.exec.name
}

output "log_group_name" {
  description = "CloudWatch Logs group name. Useful for downstream alarms or log-subscription filters."
  value       = aws_cloudwatch_log_group.this.name
}

output "log_group_arn" {
  description = "CloudWatch Logs group ARN."
  value       = aws_cloudwatch_log_group.this.arn
}

output "package_zip_path" {
  description = "Path to the deployment zip on the operator's machine. Useful for diagnostics ('what does the build think it shipped?')."
  value       = data.archive_file.package.output_path
}

output "source_hash" {
  description = "SHA-256 hash of the source files used to build the package. Bumps whenever handler.py or requirements.txt changes — drives the rebuild trigger on `null_resource.package`."
  value       = local.source_hash
}

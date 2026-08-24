###############################################################################
# Outputs — lambdas/post-confirmation module
#
# These outputs are the public contract consumed by the cognito module
# (Post-Confirmation trigger ARN) and the dev composition (debugging
# convenience). Renaming or removing any of them is a breaking change.
###############################################################################

output "function_name" {
  description = "Name of the deployed Lambda function."
  value       = aws_lambda_function.this.function_name
}

output "function_arn" {
  description = "ARN of the Post-Confirmation Lambda. Wired into the cognito module via `var.post_confirmation_lambda_arn`, which causes the User Pool's `lambda_config.post_confirmation` block + `aws_lambda_permission` to be rendered."
  value       = aws_lambda_function.this.arn
}

output "invoke_arn" {
  description = "Invoke ARN — used by API Gateway integrations. Not consumed by the cognito module (Cognito uses the function ARN directly), but exported for symmetry with the rest of the lambdas/* modules."
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

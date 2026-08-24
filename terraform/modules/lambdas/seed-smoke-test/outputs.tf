###############################################################################
# Outputs — lambdas/seed-smoke-test module
###############################################################################

output "function_name" {
  description = "Name of the deployed Lambda function. Useful for diagnostic invocations: `aws lambda invoke --function-name <this> /tmp/out.json`."
  value       = aws_lambda_function.this.function_name
}

output "function_arn" {
  description = "ARN of the seed-smoke-test Lambda. Exported for symmetry with the rest of the lambdas/* modules."
  value       = aws_lambda_function.this.arn
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
  description = "CloudWatch Logs group name. Useful for tailing the smoke-test output during apply: `aws logs tail <this> --follow`."
  value       = aws_cloudwatch_log_group.this.name
}

output "log_group_arn" {
  description = "CloudWatch Logs group ARN."
  value       = aws_cloudwatch_log_group.this.arn
}

output "package_zip_path" {
  description = "Path to the deployment zip on the operator's machine. Useful for diagnostics."
  value       = data.archive_file.package.output_path
}

output "source_hash" {
  description = "SHA-256 hash of the source files used to build the package. Bumps whenever any input changes — drives the rebuild trigger on `null_resource.package` and the re-invocation trigger on `aws_lambda_invocation.verify`."
  value       = local.source_hash
}

output "invocation_result" {
  description = "JSON body returned by the smoke test on the most recent invocation. Empty string when `invoke_on_apply = false`. The body is the structured SmokeSummary — see services/seed-smoke-test/README.md for fields. A failed smoke test fails the apply, so a non-empty value here means every assertion passed."
  value       = var.invoke_on_apply ? aws_lambda_invocation.verify[0].result : ""
}

###############################################################################
# Outputs — lambdas/indexing module
#
# Public contract consumed by the dev composition (terraform/envs/dev) and
# any observability dashboards. Renaming or removing any of these is a
# breaking change.
###############################################################################

output "lambda_arn" {
  description = "ARN of the Indexing Lambda. Use to wire alarms / pipelines / cross-account invokers."
  value       = aws_lambda_function.this.arn
}

output "lambda_function_name" {
  description = "Plain function name (not ARN) of the Indexing Lambda. Pass to dashboards or to `aws lambda invoke ...` for diagnostics."
  value       = aws_lambda_function.this.function_name
}

output "iam_role_arn" {
  description = "ARN of the Lambda's IAM execution role. Pass to the cdc-pipeline module's `consumer_lambda_role_arns` so the source SQS queue policy permits this role to ReceiveMessage."
  value       = aws_iam_role.exec.arn
}

output "iam_role_name" {
  description = "Name of the Lambda's IAM execution role. Symmetric with iam_role_arn."
  value       = aws_iam_role.exec.name
}

output "log_group_name" {
  description = "CloudWatch Logs group name. Tail with `aws logs tail <this> --follow` during the QC2 demo."
  value       = aws_cloudwatch_log_group.this.name
}

output "log_group_arn" {
  description = "CloudWatch Logs group ARN."
  value       = aws_cloudwatch_log_group.this.arn
}

output "error_alarm_arn" {
  description = "ARN of the invocation-error CloudWatch alarm. Plumb into composite alarms / SNS topics in production."
  value       = aws_cloudwatch_metric_alarm.errors.arn
}

output "error_alarm_name" {
  description = "Name of the invocation-error CloudWatch alarm."
  value       = aws_cloudwatch_metric_alarm.errors.alarm_name
}

output "dlq_alarm_arn" {
  description = "ARN of the DLQ-depth CloudWatch alarm. Fires when even one message lands in the DLQ — the Indexing Lambda's design treats a non-empty DLQ as a paging-worthy event."
  value       = aws_cloudwatch_metric_alarm.dlq_not_empty.arn
}

output "dlq_alarm_name" {
  description = "Name of the DLQ-depth CloudWatch alarm."
  value       = aws_cloudwatch_metric_alarm.dlq_not_empty.alarm_name
}

output "event_source_mapping_uuid" {
  description = "UUID of the SQS event source mapping that wires the cdc-pipeline main queue to this Lambda. Useful for `aws lambda update-event-source-mapping` runbook ops."
  value       = aws_lambda_event_source_mapping.sqs.uuid
}

output "package_zip_path" {
  description = "Path to the deployment zip on the operator's machine. Useful for diagnostics ('what does the build think it shipped?')."
  value       = data.archive_file.package.output_path
}

output "source_hash" {
  description = "SHA-256 hash of the source files used to build the package. Bumps whenever handler.py or requirements.txt changes — drives the rebuild trigger on null_resource.package."
  value       = local.source_hash
}

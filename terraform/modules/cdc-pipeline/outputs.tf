###############################################################################
# Outputs — cdc-pipeline module
#
# Public contract consumed by the Indexing_Lambda module (Task 18.1) and
# by operators / observability dashboards. Renaming or removing any of
# these is a breaking change.
###############################################################################

###############################################################################
# SQS — main FIFO queue
###############################################################################

output "main_queue_arn" {
  description = "ARN of the main FIFO queue. The Indexing_Lambda module wires this as its event source mapping."
  value       = aws_sqs_queue.cdc_main.arn
}

output "main_queue_url" {
  description = "URL of the main FIFO queue. Useful for ad-hoc diagnostics: `aws sqs get-queue-attributes --queue-url <this>`."
  value       = aws_sqs_queue.cdc_main.url
}

output "main_queue_name" {
  description = "Name of the main FIFO queue (always ends in `.fifo`)."
  value       = aws_sqs_queue.cdc_main.name
}

###############################################################################
# SQS — DLQ
###############################################################################

output "dlq_arn" {
  description = "ARN of the dead-letter queue. The Indexing_Lambda's redrive policy targets this; the CloudWatch alarm in this module monitors its depth."
  value       = aws_sqs_queue.cdc_dlq.arn
}

output "dlq_url" {
  description = "URL of the dead-letter queue. Operators use this for replay tooling: `aws sqs receive-message --queue-url <this>`."
  value       = aws_sqs_queue.cdc_dlq.url
}

output "dlq_name" {
  description = "Name of the dead-letter queue."
  value       = aws_sqs_queue.cdc_dlq.name
}

###############################################################################
# CDC Reader Lambda
###############################################################################

output "cdc_reader_function_arn" {
  description = "ARN of the CDC Reader Lambda. The EventBridge Scheduler invokes this on `var.cdc_reader_schedule_expression`. Wired into observability dashboards as the 'CDC ingest' span."
  value       = aws_lambda_function.cdc_reader.arn
}

output "cdc_reader_function_name" {
  description = "Name of the CDC Reader Lambda. Useful for operator-side `aws lambda invoke ...` diagnostic runs."
  value       = aws_lambda_function.cdc_reader.function_name
}

output "cdc_reader_role_arn" {
  description = "ARN of the CDC Reader Lambda's execution role. Exported so downstream modules can attach further policies (e.g. Secrets Manager Decrypt for an alternative credentials path)."
  value       = aws_iam_role.cdc_reader_exec.arn
}

output "cdc_reader_role_name" {
  description = "Name of the CDC Reader Lambda's execution role."
  value       = aws_iam_role.cdc_reader_exec.name
}

output "cdc_reader_log_group_name" {
  description = "CloudWatch Logs group name for the CDC Reader Lambda. Tail with `aws logs tail <this> --follow` during the QC2 demo."
  value       = aws_cloudwatch_log_group.cdc_reader.name
}

output "cdc_reader_log_group_arn" {
  description = "CloudWatch Logs group ARN."
  value       = aws_cloudwatch_log_group.cdc_reader.arn
}

###############################################################################
# Scheduler
###############################################################################

output "scheduler_name" {
  description = "EventBridge Scheduler schedule name. The schedule fires the CDC Reader Lambda on `var.cdc_reader_schedule_expression`."
  value       = aws_scheduler_schedule.cdc_reader.name
}

output "scheduler_arn" {
  description = "ARN of the EventBridge Scheduler schedule."
  value       = aws_scheduler_schedule.cdc_reader.arn
}

###############################################################################
# Alarm
###############################################################################

output "dlq_alarm_arn" {
  description = "ARN of the CloudWatch alarm that fires when the DLQ has at least one visible message. Plumb into composite alarms / SNS topics in production."
  value       = aws_cloudwatch_metric_alarm.dlq_not_empty.arn
}

output "dlq_alarm_name" {
  description = "Name of the DLQ depth alarm."
  value       = aws_cloudwatch_metric_alarm.dlq_not_empty.alarm_name
}

###############################################################################
# Convenience pass-throughs — handy for observability dashboards / IaC.
###############################################################################

output "cdc_replication_slot_name" {
  description = "Name of the Aurora logical replication slot the CDC Reader consumes. Pass-through of `var.cdc_replication_slot_name` so consumers can wire dashboards/alarms without re-declaring the constant."
  value       = var.cdc_replication_slot_name
}

output "cdc_publication_name" {
  description = "Name of the PostgreSQL publication the pgoutput plugin filters by."
  value       = var.cdc_publication_name
}

output "msk_upgrade_path_enabled" {
  description = "Echo of `var.enable_msk_upgrade_path`. Currently always false — the variable is reserved for a future task. Useful in observability / IaC reports to confirm the deployed transport (`SQS-FIFO` vs the future `MSK`)."
  value       = var.enable_msk_upgrade_path
}

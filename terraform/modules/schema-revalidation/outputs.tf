output "queue_url" {
  description = "URL of the schema-revalidation main SQS queue."
  value       = aws_sqs_queue.main.url
}

output "queue_arn" {
  description = "ARN of the schema-revalidation main SQS queue."
  value       = aws_sqs_queue.main.arn
}

output "dlq_arn" {
  description = "ARN of the schema-revalidation DLQ."
  value       = aws_sqs_queue.dlq.arn
}

output "event_rule_arn" {
  description = "ARN of the EventBridge rule for schema.version.published."
  value       = aws_cloudwatch_event_rule.schema_published.arn
}

output "function_arn" {
  description = "ARN of the Revalidation_Lambda."
  value       = module.lambda.function_arn
}

output "function_name" {
  description = "Name of the Revalidation_Lambda."
  value       = module.lambda.function_name
}

output "iam_role_arn" {
  description = "Execution role for the Revalidation_Lambda."
  value       = module.lambda.iam_role_arn
}

output "topic_prefix" {
  description = "Naming convention prefix for per-Org notification topics: <name_prefix>-notifications-<org_id>."
  value       = local.topic_prefix
}

output "default_topic_arn" {
  description = "ARN of the seeded default demo topic."
  value       = aws_sns_topic.default.arn
}

output "publisher_policy_arn" {
  description = "IAM policy ARN granting sns:Publish on any topic with the per-Org prefix. Attach to Duplicates/Lifecycle/Validation Lambdas."
  value       = aws_iam_policy.publisher.arn
}

output "manager_policy_arn" {
  description = "IAM policy ARN granting sns:CreateTopic + Subscribe on the per-Org prefix. Attach to Governance_Lambda."
  value       = aws_iam_policy.manager.arn
}

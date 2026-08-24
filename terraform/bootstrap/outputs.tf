###############################################################################
# Outputs — consumed by downstream Terraform modules' backend.tf blocks.
###############################################################################

output "state_bucket_name" {
  description = "S3 bucket holding Terraform remote state for the main stack."
  value       = aws_s3_bucket.tf_state.id
}

output "state_bucket_arn" {
  description = "ARN of the Terraform state bucket."
  value       = aws_s3_bucket.tf_state.arn
}

output "lock_table_name" {
  description = "DynamoDB table providing Terraform state locking."
  value       = aws_dynamodb_table.tf_locks.name
}

output "lock_table_arn" {
  description = "ARN of the Terraform state-lock DynamoDB table."
  value       = aws_dynamodb_table.tf_locks.arn
}

output "kms_key_arn" {
  description = "ARN of the customer-managed CMK used for S3 SSE on the state bucket."
  value       = aws_kms_key.tf_state.arn
}

output "kms_key_alias" {
  description = "Alias of the CMK used for S3 SSE on the state bucket."
  value       = aws_kms_alias.tf_state.name
}

output "backend_access_policy_arn" {
  description = "ARN of the IAM policy granting scoped read/write access to the remote-state backend."
  value       = aws_iam_policy.backend_access.arn
}

output "aws_region" {
  description = "Region the backend was provisioned in (downstream backend.tf must match)."
  value       = data.aws_region.current.name
}

output "aws_account_id" {
  description = "AWS account the backend was provisioned in."
  value       = data.aws_caller_identity.current.account_id
}

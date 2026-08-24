###############################################################################
# Outputs — opensearch module
#
# Public contract consumed by:
#   * Indexing_Lambda (Task 18.1) — writes documents to the collection
#   * Embedding_Backfill_Lambda (Task 19.1) — updates description_vec
#   * Search_Lambda (Task 28.1) — queries the collection
#   * post-apply bootstrap step (Task 10) — creates the indices using the
#     templates and the synonyms file
###############################################################################

output "collection_id" {
  description = "ID of the OpenSearch Serverless collection."
  value       = aws_opensearchserverless_collection.this.id
}

output "collection_arn" {
  description = "ARN of the OpenSearch Serverless collection. Used in Lambda execution-role IAM policies."
  value       = aws_opensearchserverless_collection.this.arn
}

output "collection_name" {
  description = "Name of the OpenSearch Serverless collection ('<name_prefix>-biodata')."
  value       = aws_opensearchserverless_collection.this.name
}

output "collection_endpoint" {
  description = "Search endpoint of the collection (https://...). This is what Lambdas issue queries against (R17)."
  value       = aws_opensearchserverless_collection.this.collection_endpoint
}

output "dashboard_endpoint" {
  description = "Dashboards (UI) endpoint of the collection. VPC-only — reachable from inside the VPC via the OpenSearch Serverless VPC endpoint."
  value       = aws_opensearchserverless_collection.this.dashboard_endpoint
}

output "synonyms_bucket" {
  description = "Name of the S3 bucket holding the biodata synonyms file (R17.3). The post-apply index-template provisioning step downloads this file and inlines it into each index's synonym filter at index-create time."
  value       = aws_s3_bucket.synonyms.bucket
}

output "synonyms_bucket_arn" {
  description = "ARN of the synonyms S3 bucket. Used in Lambda execution-role IAM policies."
  value       = aws_s3_bucket.synonyms.arn
}

output "synonyms_object_key" {
  description = "S3 object key for the uploaded synonyms file ('biodata_synonyms.txt')."
  value       = aws_s3_object.synonyms.key
}

output "synonyms_s3_uri" {
  description = "Convenience: the s3:// URI to the uploaded synonyms file. Bootstrap script consumes this directly."
  value       = "s3://${aws_s3_bucket.synonyms.bucket}/${aws_s3_object.synonyms.key}"
}

output "kms_key_arn" {
  description = "ARN of the KMS CMK encrypting the collection at rest (R31.3). Used in Lambda execution-role IAM policies for kms:Decrypt grants."
  value       = aws_kms_key.this.arn
}

output "kms_key_alias" {
  description = "Alias of the KMS CMK ('alias/<name_prefix>-opensearch')."
  value       = aws_kms_alias.this.name
}

output "vpc_endpoint_id" {
  description = "ID of the OpenSearch Serverless VPC endpoint. Surfaced for diagnostics."
  value       = aws_opensearchserverless_vpc_endpoint.this.id
}

output "index_names" {
  description = "List of indices the bootstrap step creates: data_asset, subject, instrument."
  value       = local.index_names
}

output "index_template_paths" {
  description = "Map of index name → on-disk path to its index-template JSON. Consumed by the bootstrap script (Task 10)."
  value       = local.index_template_paths
}

output "synonyms_local_file_path" {
  description = "On-disk path to the synonyms file checked into this module. Bootstrap script can read this directly when running locally instead of pulling from S3."
  value       = local.synonyms_local_file_path
}

output "encryption_policy_name" {
  description = "Name of the encryption security policy. Surfaced for diagnostics."
  value       = aws_opensearchserverless_security_policy.encryption.name
}

output "network_policy_name" {
  description = "Name of the network security policy. Surfaced for diagnostics."
  value       = aws_opensearchserverless_security_policy.network.name
}

output "data_access_policy_name" {
  description = "Name of the data access policy. Null when var.principal_arns is empty (initial bootstrap); populated once Lambda execution roles are wired."
  value       = length(aws_opensearchserverless_access_policy.data) > 0 ? aws_opensearchserverless_access_policy.data[0].name : null
}

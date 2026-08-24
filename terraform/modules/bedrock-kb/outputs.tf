output "knowledge_base_id" {
  description = "ID of the Bedrock Knowledge Base."
  value       = aws_bedrockagent_knowledge_base.this.id
}

output "knowledge_base_arn" {
  description = "ARN of the Bedrock Knowledge Base."
  value       = aws_bedrockagent_knowledge_base.this.arn
}

output "data_source_id" {
  description = "ID of the S3 data source."
  value       = aws_bedrockagent_data_source.s3.data_source_id
}

output "bucket_name" {
  description = "S3 bucket containing the KB seed content."
  value       = aws_s3_bucket.kb.bucket
}

output "bucket_arn" {
  description = "ARN of the KB S3 bucket."
  value       = aws_s3_bucket.kb.arn
}

output "collection_arn" {
  description = "ARN of the OpenSearch Serverless vector collection."
  value       = aws_opensearchserverless_collection.kb.arn
}

output "embedding_model_arn" {
  description = "ARN of the embedding model used by the KB."
  value       = local.embedding_model_arn
}

###############################################################################
# Outputs — dev environment composition
#
# Surfaces the endpoints / IDs an operator needs after `terraform apply`
# to:
#   * connect from the bastion / SSM session for diagnostics
#   * configure the React Web App build (Cognito IDs, CloudFront URL)
#   * verify the migration runner ran the schema migrations
#   * point downstream Lambda / API Gateway modules at the right targets
#     (Phase 2/3 work, but the wiring data is ready here)
###############################################################################

###############################################################################
# Aurora
###############################################################################

output "aurora_cluster_endpoint" {
  description = "Aurora writer endpoint hostname. Source of truth for every business Lambda."
  value       = module.aurora.cluster_endpoint
}

output "aurora_cluster_reader_endpoint" {
  description = "Aurora reader endpoint hostname (load-balanced across reader instances; falls back to writer when no readers exist)."
  value       = module.aurora.cluster_reader_endpoint
}

output "aurora_db_name" {
  description = "Initial database name in the Aurora cluster (default `biodata_registry`)."
  value       = module.aurora.db_name
}

output "aurora_master_secret_arn" {
  description = "Secrets Manager ARN holding the Aurora master credentials. Operators pull from here for psql access."
  value       = module.aurora.master_secret_arn
}

output "aurora_cluster_resource_id" {
  description = "Aurora cluster resource id (`cluster-xxx`). Required for IAM database authentication scoping."
  value       = module.aurora.cluster_resource_id
}

###############################################################################
# DocumentDB
###############################################################################

output "documentdb_cluster_endpoint" {
  description = "DocumentDB writer endpoint. The CDC Indexing_Lambda (Phase 2) connects here."
  value       = module.documentdb.cluster_endpoint
}

output "documentdb_reader_endpoint" {
  description = "DocumentDB reader endpoint. aind-data-access-api consumers should connect here."
  value       = module.documentdb.reader_endpoint
}

output "documentdb_master_secret_arn" {
  description = "Secrets Manager ARN holding the DocumentDB master credentials (Indexing_Lambda usage)."
  value       = module.documentdb.master_secret_arn
}

output "documentdb_readonly_secret_arn" {
  description = "Secrets Manager ARN holding the read-only DB user credentials. aind-data-access-api consumers receive IAM access only to this secret."
  value       = module.documentdb.readonly_secret_arn
}

###############################################################################
# OpenSearch
###############################################################################

output "opensearch_collection_endpoint" {
  description = "OpenSearch Serverless collection search endpoint (https://...). Search_Lambda (Phase 3) issues queries here."
  value       = module.opensearch.collection_endpoint
}

output "opensearch_collection_arn" {
  description = "OpenSearch Serverless collection ARN. Used by Lambda execution-role IAM policies."
  value       = module.opensearch.collection_arn
}

output "opensearch_synonyms_s3_uri" {
  description = "S3 URI of the uploaded biodata synonyms file. Index-template provisioning script reads this."
  value       = module.opensearch.synonyms_s3_uri
}

###############################################################################
# ElastiCache
###############################################################################

output "elasticache_primary_endpoint" {
  description = "Redis primary endpoint hostname. Lambdas connect here over TLS using the AUTH token from Secrets Manager."
  value       = module.elasticache.primary_endpoint
}

output "elasticache_auth_token_secret_arn" {
  description = "Secrets Manager ARN holding the Redis AUTH token + host/port."
  value       = module.elasticache.auth_token_secret_arn
}

###############################################################################
# Cognito
###############################################################################

output "cognito_user_pool_id" {
  description = "Cognito User Pool ID. Web App + Authorizer_Lambda consume this."
  value       = module.cognito.user_pool_id
}

output "cognito_user_pool_arn" {
  description = "Cognito User Pool ARN. API Gateway authorizer wiring (Phase 2) uses this."
  value       = module.cognito.user_pool_arn
}

output "cognito_user_pool_client_id" {
  description = "Cognito User Pool client ID for the public Web App (OAuth Authorization Code with PKCE)."
  value       = module.cognito.user_pool_client_id
}

output "cognito_hosted_ui_domain" {
  description = "Cognito Hosted UI HTTPS domain. The Web App redirects here for sign-in."
  value       = module.cognito.hosted_ui_domain
}

output "cognito_jwt_issuer" {
  description = "JWT issuer URL for Authorizer_Lambda token validation."
  value       = module.cognito.jwt_issuer
}

###############################################################################
# CloudFront / Web App
###############################################################################

output "cloudfront_distribution_id" {
  description = "CloudFront distribution ID. Use with `aws cloudfront create-invalidation` after deploying a new build."
  value       = module.cloudfront_s3.distribution_id
}

output "cloudfront_distribution_domain" {
  description = "CloudFront-provided default domain (e.g. d111111abcdef8.cloudfront.net). The QC1 demo URL."
  value       = module.cloudfront_s3.distribution_domain
}

output "webapp_bucket_name" {
  description = "S3 bucket holding the React app artifacts. `aws s3 sync dist/ s3://<this>/` after a frontend build."
  value       = module.cloudfront_s3.bucket_name
}

output "acm_validation_records" {
  description = "DNS CNAME records the customer must add to their zone to validate the ACM certificate. Empty list when var.enable_custom_domain is false (the distribution then uses the CloudFront default cert)."
  value       = module.cloudfront_s3.acm_validation_records
}

###############################################################################
# Migration runner
###############################################################################

output "migration_runner_function_name" {
  description = "Name of the deployed migration runner Lambda. Operators can re-invoke manually with `aws lambda invoke --function-name <this> /tmp/out.json` for diagnostic runs."
  value       = module.migration_runner.function_name
}

output "migration_runner_invocation_result" {
  description = "JSON body returned by the most recent migration runner invocation. Shape: {applied: [...], skipped: [...], drift: [...], schema_version_created: bool, elapsed_ms: int}. On a clean account the first apply returns the full migration list under `applied`; subsequent applies return them under `skipped` (idempotency check)."
  value       = module.migration_runner.invocation_result
}

###############################################################################
# Post-confirmation Lambda
###############################################################################

output "post_confirmation_lambda_function_name" {
  description = "Name of the Cognito Post-Confirmation Lambda."
  value       = module.post_confirmation_lambda.function_name
}

output "post_confirmation_lambda_function_arn" {
  description = "ARN of the Cognito Post-Confirmation Lambda. Wired into the Cognito User Pool's lambda_config.post_confirmation."
  value       = module.post_confirmation_lambda.function_arn
}

###############################################################################
# Network — useful for diagnostics + Phase 2 module wiring.
###############################################################################

output "vpc_id" {
  description = "VPC ID. Phase 2 modules (CDC pipeline, business Lambdas) consume this."
  value       = module.vpc.vpc_id
}

output "private_subnet_ids" {
  description = "Private subnet IDs. Phase 2 Lambda modules attach here."
  value       = module.vpc.private_subnet_ids
}

output "internal_security_group_id" {
  description = "Internal security group ID. The baseline SG every VPC-attached resource (and Phase 2 Lambda) joins."
  value       = module.vpc.internal_security_group_id
}

###############################################################################
# Resolved identity — confirms who provisioned the stack.
###############################################################################

output "aws_account_id" {
  description = "AWS account the dev composition is deployed into."
  value       = data.aws_caller_identity.current.account_id
}

output "aws_region" {
  description = "AWS region the dev composition is deployed into."
  value       = data.aws_region.current.name
}

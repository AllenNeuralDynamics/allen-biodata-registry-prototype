###############################################################################
# Outputs — elasticache module
#
# Public contract consumed by Lambda execution-role policies (Secrets
# Manager + KMS access) and by the shared Lambda Layer (host/port/auth at
# runtime). Renaming or removing any of these is a breaking change.
###############################################################################

output "replication_group_id" {
  description = "ID of the Redis replication group."
  value       = aws_elasticache_replication_group.this.replication_group_id
}

output "replication_group_arn" {
  description = "ARN of the Redis replication group. Useful for IAM resource scoping in execution-role policies and for CloudWatch alarms."
  value       = aws_elasticache_replication_group.this.arn
}

output "primary_endpoint" {
  description = "Primary endpoint hostname of the Redis replication group. Lambdas connect here over TLS using the AUTH token from Secrets Manager. Failover (~30s) automatically re-points this DNS at the new primary."
  value       = aws_elasticache_replication_group.this.primary_endpoint_address
}

output "reader_endpoint" {
  description = "Reader endpoint hostname (load-balanced across replicas when num_cache_clusters >= 2). With the PoC default of 1 primary + 1 replica, this targets the replica."
  value       = aws_elasticache_replication_group.this.reader_endpoint_address
}

output "configuration_endpoint" {
  description = "Configuration endpoint hostname (only populated for cluster-mode-enabled replication groups). Empty in cluster-mode-disabled deployments — which is the PoC default."
  value       = aws_elasticache_replication_group.this.configuration_endpoint_address
}

output "port" {
  description = "Listener port (default 6379)."
  value       = aws_elasticache_replication_group.this.port
}

output "engine_version_actual" {
  description = "Actual engine version running on the cluster, may differ from the requested minor patch."
  value       = aws_elasticache_replication_group.this.engine_version_actual
}

output "subnet_group_name" {
  description = "Name of the ElastiCache subnet group."
  value       = aws_elasticache_subnet_group.this.name
}

output "parameter_group_name" {
  description = "Name of the ElastiCache parameter group (family redis7, allkeys-lru eviction, 5-min client idle timeout)."
  value       = aws_elasticache_parameter_group.this.name
}

output "auth_token_secret_arn" {
  description = "ARN of the Secrets Manager secret holding `{auth_token, host, port}`. Lambda execution roles must be granted secretsmanager:GetSecretValue on this ARN and kms:Decrypt on kms_key_arn to read it."
  value       = aws_secretsmanager_secret.redis_auth.arn
}

output "auth_token_secret_name" {
  description = "Name of the Secrets Manager secret. Lambdas can resolve by name as well as ARN."
  value       = aws_secretsmanager_secret.redis_auth.name
}

output "kms_key_arn" {
  description = "ARN of the KMS CMK encrypting the replication group at rest and the AUTH-token secret. Lambda execution roles need kms:Decrypt on this ARN."
  value       = aws_kms_key.this.arn
}

output "kms_key_alias" {
  description = "Alias of the KMS CMK (e.g. alias/biodata-registry-dev-redis). Convenient for human-readable references; IAM policies should use the ARN."
  value       = aws_kms_alias.this.name
}

output "security_group_id" {
  description = "First security group ID attached to the replication group (the SG Lambdas should add to their egress rules to reach Redis on port 6379). The full list is var.security_group_ids; the typical case is a single SG so this output is a convenience."
  value       = var.security_group_ids[0]
}

###############################################################################
# Outputs — aurora module
#
# These outputs are the public contract consumed by every business Lambda
# (Registration, Validation, Lifecycle, ...), the migration runner
# (Task 8.1), and the CDC pipeline (Task 17.1). Renaming or removing any
# of them is a breaking change.
###############################################################################

output "cluster_id" {
  description = "Aurora cluster identifier (logical id, not ARN). Use for cross-module references that expect a string id."
  value       = aws_rds_cluster.this.id
}

output "cluster_arn" {
  description = "ARN of the Aurora cluster."
  value       = aws_rds_cluster.this.arn
}

output "cluster_resource_id" {
  description = "Immutable cluster resource id (`cluster-xxx`). Required when granting IAM database authentication."
  value       = aws_rds_cluster.this.cluster_resource_id
}

output "cluster_endpoint" {
  description = "Writer endpoint hostname. All write paths (Registration, Lifecycle, Governance, migration runner) connect here."
  value       = aws_rds_cluster.this.endpoint
}

output "cluster_reader_endpoint" {
  description = "Read-only Aurora endpoint that load-balances across reader instances. Useful for the Search_Lambda fallback path and for Observability metrics."
  value       = aws_rds_cluster.this.reader_endpoint
}

output "db_name" {
  description = "Initial database name created inside the cluster (default `biodata_registry`)."
  value       = aws_rds_cluster.this.database_name
}

output "port" {
  description = "TCP port the cluster listens on (5432 for PostgreSQL)."
  value       = aws_rds_cluster.this.port
}

output "master_username" {
  description = "Master username — the Secrets Manager secret is the authoritative source; this is exported only for diagnostics."
  value       = aws_rds_cluster.this.master_username
  sensitive   = true
}

output "master_secret_arn" {
  description = "Secrets Manager secret ARN holding the master credentials. Lambdas resolve this at runtime to obtain {username, password, host, port, dbname, engine, cdc_replication_slot}."
  value       = aws_secretsmanager_secret.master.arn
}

output "master_secret_name" {
  description = "Secrets Manager secret name (e.g. <name_prefix>/aurora/master-credentials). Convenient for IAM resource-level grants."
  value       = aws_secretsmanager_secret.master.name
}

output "master_secret_version_id" {
  description = "Version id of the current Secrets Manager secret value. Useful as an etag-like signal for downstream resources that want to be replaced when the password rotates."
  value       = aws_secretsmanager_secret_version.master.version_id
}

output "kms_key_arn" {
  description = "ARN of the CMK used to encrypt cluster storage and the credentials secret. Either the externally provided `var.kms_key_arn` or the module-created CMK."
  value       = local.effective_kms_key_arn
}

output "kms_key_managed_by_module" {
  description = "True when the module created the CMK; false when an external CMK was provided. Useful for downstream modules that need to know whether they own the key lifecycle."
  value       = var.kms_key_arn == null
}

output "parameter_group_name" {
  description = "Name of the cluster parameter group (logical-replication enabled, pgvector + pg_stat_statements preloaded)."
  value       = aws_rds_cluster_parameter_group.this.name
}

output "cluster_parameter_group_name" {
  description = "Alias of `parameter_group_name` — clarifies that this is the *cluster*-level parameter group (vs. the per-instance `db_parameter_group_name`)."
  value       = aws_rds_cluster_parameter_group.this.name
}

output "db_parameter_group_name" {
  description = "Name of the DB instance parameter group. Carries instance-level settings such as `log_min_duration_statement = 1000` for slow-query logging."
  value       = aws_db_parameter_group.this.name
}

output "db_subnet_group_name" {
  description = "Name of the DB subnet group spanning the VPC's private subnets. Exported so other database modules (or read replicas) can reuse the same subnet boundary if desired."
  value       = aws_db_subnet_group.this.name
}

output "security_group_ids" {
  description = "Security group IDs attached to the cluster. Pass-through of var.security_group_ids for downstream visibility."
  value       = var.security_group_ids
}

output "instance_identifiers" {
  description = "List of cluster instance identifiers (writer first, then any readers)."
  value       = aws_rds_cluster_instance.this[*].identifier
}

output "cdc_replication_slot_name" {
  description = "Name of the logical replication slot the CDC pipeline (Task 17.1) consumes. Tracked here so the cdc-pipeline module can wire the source without duplicating the constant."
  value       = var.cdc_replication_slot_name
}

# Alias kept for clarity — downstream modules sometimes refer to this as
# `replication_slot_name`. Both names point to the same value.
output "replication_slot_name" {
  description = "Alias of `cdc_replication_slot_name`. Convenient for cdc-pipeline / Indexing_Lambda wiring."
  value       = var.cdc_replication_slot_name
}

output "iam_database_authentication_enabled" {
  description = "True when IAM database authentication is enabled on the cluster. Lambdas can use `aws rds generate-db-auth-token` to obtain short-lived tokens instead of the long-lived password from Secrets Manager."
  value       = aws_rds_cluster.this.iam_database_authentication_enabled
}

output "monitoring_role_arn" {
  description = "ARN of the IAM role used for Enhanced Monitoring (null when monitoring_interval_seconds = 0)."
  value       = var.monitoring_interval_seconds > 0 ? aws_iam_role.rds_monitoring[0].arn : null
}

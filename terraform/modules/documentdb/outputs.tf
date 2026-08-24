###############################################################################
# Outputs — documentdb module
#
# Public contract consumed by:
#   * Indexing_Lambda (CDC consumer) — connects with master credential
#   * `aind-data-access-api` consumer Lambdas / EC2 — connect with the
#     read-only credential exposed via `readonly_secret_arn`
#
# Both `master_password` and `readonly_password` are intentionally NOT
# exported. Consumers MUST fetch them from Secrets Manager.
###############################################################################

output "cluster_endpoint" {
  description = "Writer endpoint (cluster endpoint). Use for write workloads (the CDC Indexing_Lambda)."
  value       = aws_docdb_cluster.this.endpoint
}

output "reader_endpoint" {
  description = "Reader endpoint. The aind-data-access-api consumers should connect here so reads are load-balanced across the replicas (when more than one instance is running)."
  value       = aws_docdb_cluster.this.reader_endpoint
}

output "port" {
  description = "TCP port the cluster listens on. Always 27017 for DocumentDB."
  value       = aws_docdb_cluster.this.port
}

output "cluster_arn" {
  description = "ARN of the DocumentDB cluster."
  value       = aws_docdb_cluster.this.arn
}

output "cluster_resource_id" {
  description = "Immutable DocumentDB cluster resource ID. Useful for IAM resource-level grants and CloudTrail correlation."
  value       = aws_docdb_cluster.this.cluster_resource_id
}

output "master_secret_arn" {
  description = "ARN of the Secrets Manager secret holding master credentials. The Indexing_Lambda and the bootstrap workflow have IAM access to this secret. The aind-data-access-api consumers MUST NOT receive access to this secret."
  value       = aws_secretsmanager_secret.master.arn
}

output "readonly_secret_arn" {
  description = "ARN of the Secrets Manager secret holding read-only credentials. The aind-data-access-api consumers receive IAM access ONLY to this secret — never the master. JSON value: {username, password, host, reader_host, port, engine, dbName, ssl, sslCABundleUrl, role}."
  value       = aws_secretsmanager_secret.readonly.arn
}

output "kms_key_arn" {
  description = "KMS CMK ARN used for storage and Secrets encryption (either the one passed in or the one this module created)."
  value       = local.effective_kms_key_arn
}

output "security_group_id" {
  description = "First entry of the input security_group_ids list. Convenience output for downstream callers that want a single id (the cluster ENIs may be attached to multiple SGs; index 0 is the canonical one used by the Indexing_Lambda for SG-to-SG references)."
  value       = var.security_group_ids[0]
}

output "tls_ca_bundle_url" {
  description = "URL where consumers must download the RDS/DocumentDB CA bundle (`global-bundle.pem`). DocumentDB requires TLS by default in 5.0; clients must pass `tlsCAFile=global-bundle.pem` (or equivalent) when connecting. The cert is NOT bundled with the cluster — it must be fetched out-of-band."
  value       = var.tls_ca_bundle_url
}

###############################################################################
# Secondary outputs — convenience pass-through for diagnostics and tests.
###############################################################################

output "cluster_id" {
  description = "Cluster identifier (e.g. biodata-registry-dev-documentdb)."
  value       = aws_docdb_cluster.this.cluster_identifier
}

output "instance_endpoints" {
  description = "Per-instance endpoints (one per cluster instance). Useful for diagnostics."
  value       = aws_docdb_cluster_instance.this[*].endpoint
}

output "instance_identifiers" {
  description = "Per-instance identifiers."
  value       = aws_docdb_cluster_instance.this[*].identifier
}

output "master_secret_name" {
  description = "Friendly name of the master credentials secret (`{name_prefix}-documentdb-master`)."
  value       = aws_secretsmanager_secret.master.name
}

output "readonly_secret_name" {
  description = "Friendly name of the read-only credentials secret (`{name_prefix}-documentdb-readonly`)."
  value       = aws_secretsmanager_secret.readonly.name
}

output "parameter_group_name" {
  description = "Name of the cluster parameter group (audit_logs + tls enabled)."
  value       = aws_docdb_cluster_parameter_group.this.name
}

output "db_subnet_group_name" {
  description = "Name of the DB subnet group spanning the private subnets."
  value       = aws_docdb_subnet_group.this.name
}

output "master_username" {
  description = "Master DB username (default `docdb_admin`)."
  value       = var.master_username
}

output "readonly_username" {
  description = "Read-only DB username (default `biodata_reader`). Created by the post-apply mongosh bootstrap when var.enable_readonly_user_bootstrap is true."
  value       = var.readonly_username
}

output "db_name" {
  description = "Logical database name carried on both Secrets Manager secrets. The Indexing_Lambda writes its DocumentDB collections into this database."
  value       = var.db_name
}

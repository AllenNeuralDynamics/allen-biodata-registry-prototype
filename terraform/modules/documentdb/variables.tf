###############################################################################
# Variables — documentdb module
#
# DocumentDB is the MongoDB-compatible read layer fed by the CDC pipeline
# (Aurora WAL → Indexing_Lambda → DocumentDB). It preserves the existing
# `aind-data-access-api` query shape so the Allen Institute's tools keep
# working unchanged. See design.md §Data Models.DocumentDB Document Shape and
# §Design Decisions.DocumentDB Access Model and Trust Boundary.
###############################################################################

variable "name_prefix" {
  description = "Prefix applied to every resource name. Typically '<project>-<environment>', e.g. 'biodata-registry-dev'."
  type        = string

  validation {
    condition     = length(var.name_prefix) > 0 && length(var.name_prefix) <= 40
    error_message = "name_prefix must be 1–40 characters."
  }
}

variable "environment" {
  description = "Environment tag applied to every resource (dev, staging, prod)."
  type        = string
  default     = "dev"
}

variable "project" {
  description = "Project tag applied to every resource."
  type        = string
  default     = "biodata-registry"
}

variable "vpc_id" {
  description = "ID of the VPC the cluster is launched in. Sourced from the vpc module's `vpc_id` output."
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnet IDs for the DocumentDB DB subnet group. Must span ≥2 AZs. Sourced from the vpc module's `private_subnet_ids` output."
  type        = list(string)

  validation {
    condition     = length(var.private_subnet_ids) >= 2
    error_message = "DocumentDB requires at least two subnets in distinct AZs."
  }
}

variable "security_group_ids" {
  description = "Security groups attached to the cluster ENIs. The vpc module's `internal_security_group_id` is the expected default (intra-VPC connectivity)."
  type        = list(string)

  validation {
    condition     = length(var.security_group_ids) >= 1
    error_message = "Provide at least one security group id."
  }
}

variable "kms_key_arn" {
  description = "Optional pre-existing KMS CMK ARN used for storage and Secrets Manager encryption. If null (default) the module provisions a dedicated CMK with key rotation enabled. Required by R31.3."
  type        = string
  default     = null
}

variable "db_name" {
  description = "Logical database name used by `aind-data-access-api`. The Indexing_Lambda writes its DocumentDB collections into this database, and the read-only DB user is granted the `read` role scoped to it."
  type        = string
  default     = "biodata_registry"

  validation {
    condition     = can(regex("^[A-Za-z][A-Za-z0-9_]{0,62}$", var.db_name))
    error_message = "db_name must start with a letter and contain only letters, digits, and underscores (max 63 chars)."
  }
}

variable "master_username" {
  description = "Master username for the DocumentDB cluster. Stored in the master Secrets Manager secret."
  type        = string
  default     = "docdb_admin"

  validation {
    condition     = can(regex("^[A-Za-z][A-Za-z0-9_]{0,15}$", var.master_username))
    error_message = "master_username must start with a letter, be 1-16 chars, and contain only letters, digits, and underscores."
  }
}

variable "instance_class" {
  description = "DocumentDB instance class. `db.r6g.large` is the smallest current-generation class supported by DocumentDB 5.0 in production (DocumentDB 5.0 dropped the `db.t3.medium` class that 4.0 supported). DocumentDB has no Serverless option for v5 yet — see README cost section."
  type        = string
  default     = "db.r6g.large"
}

variable "instance_count" {
  description = "Number of cluster instances. PoC default is 1 (no HA — see README trade-off). Production should run ≥2 instances spread across AZs for failover."
  type        = number
  default     = 1

  validation {
    condition     = var.instance_count >= 1 && var.instance_count <= 16
    error_message = "instance_count must be between 1 and 16."
  }
}

variable "engine_version" {
  description = "DocumentDB engine version. `5.0.0` is the engine that exposes the MongoDB 5.0 wire protocol — what `aind-data-access-api` is tested against."
  type        = string
  default     = "5.0.0"
}

variable "backup_retention_period" {
  description = "Days to retain automated backups. PoC default is 7."
  type        = number
  default     = 7

  validation {
    condition     = var.backup_retention_period >= 1 && var.backup_retention_period <= 35
    error_message = "backup_retention_period must be between 1 and 35 days."
  }
}

variable "preferred_backup_window" {
  description = "Daily backup window in UTC (HH:MM-HH:MM)."
  type        = string
  default     = "07:00-08:00"
}

variable "preferred_maintenance_window" {
  description = "Weekly maintenance window in UTC (ddd:HH:MM-ddd:HH:MM)."
  type        = string
  default     = "sun:09:00-sun:10:00"
}

variable "skip_final_snapshot" {
  description = "If true (PoC default), `terraform destroy` will not create a final snapshot. Set to false in production."
  type        = bool
  default     = true
}

variable "deletion_protection" {
  description = "If true, RDS deletion protection is enabled and the cluster cannot be destroyed until the flag is flipped. PoC default `false`; production MUST set `true`."
  type        = bool
  default     = false
}

variable "apply_immediately" {
  description = "If true (PoC default), parameter changes apply on the next reboot rather than during the maintenance window. Convenient for the PoC; risky in production."
  type        = bool
  default     = true
}

variable "secret_recovery_window_in_days" {
  description = "Recovery window for Secrets Manager soft-delete. 7 days for the PoC; production should use 30."
  type        = number
  default     = 7

  validation {
    condition     = var.secret_recovery_window_in_days == 0 || (var.secret_recovery_window_in_days >= 7 && var.secret_recovery_window_in_days <= 30)
    error_message = "secret_recovery_window_in_days must be 0 (immediate deletion) or between 7 and 30."
  }
}

variable "enabled_cloudwatch_log_exports" {
  description = "DocumentDB log types to export to CloudWatch. `audit` is required for the production-hardening path described in design.md (post-hoc detection of queries that omit the RLS-equivalent client filter); `profiler` aids slow-query diagnosis."
  type        = list(string)
  default     = ["audit", "profiler"]
}

variable "readonly_username" {
  description = "Read-only DB username created by the post-apply mongosh bootstrap. The `aind-data-access-api` consumers connect as this user."
  type        = string
  default     = "biodata_reader"

  validation {
    condition     = can(regex("^[A-Za-z][A-Za-z0-9_]{0,30}$", var.readonly_username))
    error_message = "readonly_username must start with a letter and be 1-31 chars (letters, digits, underscores)."
  }
}

variable "enable_readonly_user_bootstrap" {
  description = "If true (default), runs a `null_resource` + `local-exec` after the cluster is up that uses `mongosh` to create the read-only DB user and writes its credentials into the read-only Secrets Manager secret. Requires `mongosh` on PATH and VPC reach to the cluster (operator workstation must be on a bastion or VPN). Disable to skip the bootstrap and run the script manually post-apply."
  type        = bool
  default     = true
}

variable "mongosh_binary" {
  description = "Path to the mongosh binary used by the read-only-user bootstrap local-exec. Override if `mongosh` is not on PATH (e.g. installed under /opt/homebrew/bin)."
  type        = string
  default     = "mongosh"
}

variable "tls_ca_bundle_url" {
  description = "URL the operator must download the RDS/DocumentDB CA bundle from. Exposed as a module output so consumers know where to fetch `global-bundle.pem`."
  type        = string
  default     = "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"
}

variable "tags" {
  description = "Additional tags merged onto every resource. Project / Environment / Module are added automatically."
  type        = map(string)
  default     = {}
}

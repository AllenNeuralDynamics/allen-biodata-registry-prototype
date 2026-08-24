###############################################################################
# Variables — aurora module
#
# Defaults are tuned for the Allen BioData Registry PoC (us-west-2, dev env).
# Every value can be overridden by the consuming environment composition.
###############################################################################

variable "name_prefix" {
  description = "Prefix applied to every resource name. Typically '<project>-<environment>', e.g. 'biodata-registry-dev'."
  type        = string
  default     = "biodata-registry-dev"

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
  description = "ID of the VPC the cluster will be launched in. Sourced from the `vpc` module's `vpc_id` output."
  type        = string

  validation {
    condition     = length(var.vpc_id) > 0
    error_message = "vpc_id is required."
  }
}

variable "private_subnet_ids" {
  description = "Private subnet IDs the DB subnet group will span. Provide at least two for Aurora's multi-AZ requirement; sourced from the `vpc` module's `private_subnet_ids` output."
  type        = list(string)

  validation {
    condition     = length(var.private_subnet_ids) >= 2
    error_message = "Aurora requires at least two private subnets across different AZs."
  }
}

variable "security_group_ids" {
  description = "VPC security group IDs to attach to the cluster's network interfaces. Typically the `internal_security_group_id` from the `vpc` module — Lambdas in the same SG then reach Aurora over port 5432."
  type        = list(string)

  validation {
    condition     = length(var.security_group_ids) >= 1
    error_message = "At least one security group must be provided."
  }
}

variable "kms_key_arn" {
  description = "Optional ARN of a customer-managed KMS CMK used to encrypt Aurora storage and the Secrets Manager secret. If null, the module creates a dedicated CMK and exports its ARN. Provide an external CMK in production environments where key lifecycle is managed centrally."
  type        = string
  default     = null
}

variable "engine_version" {
  description = "Aurora PostgreSQL engine version. Default 16.13 is the latest stable 16.x available in us-west-2 at the time of authoring (confirmed via `aws rds describe-db-engine-versions --engine aurora-postgresql`); pgvector is bundled with 16.x. The module uses the standard `aurora-postgresql` engine — NOT `-limitless`."
  type        = string
  default     = "16.13"
}

variable "db_name" {
  description = "Initial database name created inside the cluster. Aurora rejects hyphens here, so the default uses underscores."
  type        = string
  default     = "biodata_registry"

  validation {
    condition     = can(regex("^[A-Za-z][A-Za-z0-9_]{0,62}$", var.db_name))
    error_message = "db_name must start with a letter and contain only letters, digits, and underscores (max 63 chars)."
  }
}

variable "master_username" {
  description = "Master username for the cluster. Stored in Secrets Manager and rotated separately in production."
  type        = string
  default     = "biodata_admin"

  validation {
    condition     = can(regex("^[A-Za-z][A-Za-z0-9_]{0,15}$", var.master_username))
    error_message = "master_username must start with a letter, be 1-16 chars, and contain only letters, digits, and underscores."
  }
}

variable "min_capacity_acu" {
  description = "Serverless v2 minimum Aurora Capacity Units (ACU). 0.5 is the floor; each 0.5 ACU is roughly 1 GiB of memory. PoC default keeps idle cost near the ~$43/mo floor."
  type        = number
  default     = 0.5

  validation {
    condition     = var.min_capacity_acu >= 0.5 && var.min_capacity_acu <= 128
    error_message = "min_capacity_acu must be between 0.5 and 128."
  }
}

variable "max_capacity_acu" {
  description = "Serverless v2 maximum ACU. 4.0 is more than enough for PoC traffic; raise to 16+ for production."
  type        = number
  default     = 4.0

  validation {
    condition     = var.max_capacity_acu >= 1 && var.max_capacity_acu <= 128
    error_message = "max_capacity_acu must be between 1 and 128."
  }
}

variable "instance_count" {
  description = "Number of Aurora Serverless v2 instances to create. The first is the writer; any additional instances are reader replicas. PoC default is 1 (writer only)."
  type        = number
  default     = 1

  validation {
    condition     = var.instance_count >= 1 && var.instance_count <= 15
    error_message = "instance_count must be between 1 and 15."
  }
}

variable "backup_retention_days" {
  description = "Automated backup retention window in days. PoC default 7; production should use 30."
  type        = number
  default     = 7

  validation {
    condition     = var.backup_retention_days >= 1 && var.backup_retention_days <= 35
    error_message = "backup_retention_days must be between 1 and 35."
  }
}

variable "preferred_backup_window" {
  description = "Daily backup window in UTC, format hh24:mi-hh24:mi. Default 06:00-07:00 UTC keeps backups off Allen Institute working hours (Pacific) — that's 22:00-23:00 PST / 23:00-00:00 PDT."
  type        = string
  default     = "06:00-07:00"
}

variable "preferred_maintenance_window" {
  description = "Weekly maintenance window in UTC, format ddd:hh24:mi-ddd:hh24:mi. Default Sunday 08:00-09:00 UTC (00:00-01:00 PST / 01:00-02:00 PDT) — clearly separated from the backup window above."
  type        = string
  default     = "sun:08:00-sun:09:00"
}

variable "skip_final_snapshot" {
  description = "Whether to skip the final snapshot when the cluster is destroyed. PoC default `true` for cheap teardown; production environments MUST set this to `false`."
  type        = bool
  default     = true
}

variable "deletion_protection" {
  description = "Enable RDS deletion protection on the cluster. PoC default `false` so `terraform destroy` works cleanly; production environments MUST set this to `true`."
  type        = bool
  default     = false
}

variable "secrets_recovery_window_days" {
  description = "Recovery window for the Secrets Manager secret. PoC default 7 days; production should be 30. Set to 0 to delete immediately (test only)."
  type        = number
  default     = 7

  validation {
    condition     = var.secrets_recovery_window_days == 0 || (var.secrets_recovery_window_days >= 7 && var.secrets_recovery_window_days <= 30)
    error_message = "secrets_recovery_window_days must be 0 or between 7 and 30."
  }
}

variable "cdc_replication_slot_name" {
  description = "Name of the logical replication slot the CDC pipeline (Task 17.1) consumes. The slot itself is created post-apply by the `null_resource.bootstrap_slot_and_extensions` provisioner in this module — see README for the prerequisite that the operator can reach the Aurora endpoint via psql (typically from an SSM session, Cloud9, or VPN inside the VPC)."
  type        = string
  default     = "biodata_cdc"
}

variable "iam_database_authentication_enabled" {
  description = "Enable IAM database authentication on the cluster. Free, doesn't conflict with master password auth, and lets downstream Lambdas use IAM-issued tokens instead of long-lived Secrets Manager creds in production."
  type        = bool
  default     = true
}

variable "performance_insights_enabled" {
  description = "Enable Performance Insights on every cluster instance. Enabled by default; the cluster's CMK is reused for PI encryption."
  type        = bool
  default     = true
}

variable "monitoring_interval_seconds" {
  description = "Enhanced Monitoring granularity in seconds (0, 1, 5, 10, 15, 30, 60). 60s is the default per Aurora best practice — frequent enough to spot pressure without doubling the per-instance CloudWatch cost."
  type        = number
  default     = 60

  validation {
    condition     = contains([0, 1, 5, 10, 15, 30, 60], var.monitoring_interval_seconds)
    error_message = "monitoring_interval_seconds must be one of 0, 1, 5, 10, 15, 30, or 60."
  }
}

variable "bootstrap_slot_via_null_resource" {
  description = "When true (default), the module runs a `null_resource` with a `local-exec` provisioner that connects via psql to create the CDC logical replication slot, the `vector` extension, and the `pg_trgm` extension after the writer instance is up. Disable when running terraform apply from outside the VPC and have the migration runner (Task 8.1) or a Lambda bootstrapper handle bootstrap instead — see README."
  type        = bool
  default     = true
}

variable "tags" {
  description = "Additional tags merged onto every resource. Project / Environment are added automatically."
  type        = map(string)
  default     = {}
}

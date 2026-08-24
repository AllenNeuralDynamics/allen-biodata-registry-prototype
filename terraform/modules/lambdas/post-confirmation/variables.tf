###############################################################################
# Variables — lambdas/post-confirmation module
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

variable "tags" {
  description = "Additional tags merged onto every resource."
  type        = map(string)
  default     = {}
}

###############################################################################
# Source / packaging
###############################################################################

variable "source_dir" {
  description = "Absolute path to the Lambda source directory (the directory containing handler.py and requirements.txt). The dev composition typically supplies a path like '$${path.module}/../../../services/post-confirmation-lambda' or an equivalent absolute path."
  type        = string
}

variable "build_dir" {
  description = "Absolute path to a working directory the module owns for staging the deployment package. Anything under this path may be deleted and recreated on every apply. Defaults to a per-module temp directory under the calling Terraform working directory."
  type        = string
  default     = null
}

variable "python_executable" {
  description = "Python executable used to install runtime dependencies into the staging directory. Defaults to 'python3'. The interpreter version should match the Lambda runtime (3.12) to ensure native deps — if any — produce compatible wheels. pg8000 is pure-Python so any 3.x interpreter works."
  type        = string
  default     = "python3"
}

###############################################################################
# Aurora connection (env vars)
###############################################################################

variable "db_host" {
  description = "Aurora writer endpoint hostname. From `module.aurora.cluster_endpoint`."
  type        = string
}

variable "db_port" {
  description = "Aurora TCP port (5432 for PostgreSQL)."
  type        = number
  default     = 5432
}

variable "db_name" {
  description = "Aurora database name. From `module.aurora.db_name`."
  type        = string
}

variable "db_user" {
  description = "Aurora database user that the Lambda authenticates as. The user must (a) exist, (b) be granted membership in the `rds_iam` role for IAM database authentication, and (c) have INSERT on the `app_user` table. Created by the schema migration runner (Task 8.1)."
  type        = string
}

variable "aurora_cluster_resource_id" {
  description = "Immutable Aurora cluster resource id (`cluster-xxx`). Used to scope the IAM policy granting `rds-db:connect` to *this* cluster + DB user, not all clusters in the account. From `module.aurora.cluster_resource_id`."
  type        = string
}

variable "aurora_cluster_arn" {
  description = "Aurora cluster ARN. Used in module documentation only — the IAM policy uses the resource id form. From `module.aurora.cluster_arn`."
  type        = string
  default     = null
}

variable "app_user_has_org_id" {
  description = "Set to `true` once migration 7.1 adds an `org_id` column to `app_user`. When `true`, the Lambda includes `custom:org_id` from the Cognito event in its INSERT statement. Defaults to `false` so the module deploys cleanly against the current schema."
  type        = bool
  default     = false
}

###############################################################################
# Networking
###############################################################################

variable "vpc_subnet_ids" {
  description = "Private subnet IDs the Lambda runs in. Must include the subnets that route to Aurora — the Lambda needs network reach to the writer endpoint over port 5432. From `module.vpc.private_subnet_ids`."
  type        = list(string)

  validation {
    condition     = length(var.vpc_subnet_ids) > 0
    error_message = "At least one private subnet ID is required."
  }
}

variable "vpc_security_group_ids" {
  description = "Security group IDs attached to the Lambda's ENIs. The SGs must permit egress to Aurora's security group on port 5432. The dev composition typically reuses the Aurora client SG from `module.vpc`."
  type        = list(string)

  validation {
    condition     = length(var.vpc_security_group_ids) > 0
    error_message = "At least one security group ID is required."
  }
}

###############################################################################
# Runtime / sizing
###############################################################################

variable "memory_mb" {
  description = "Lambda memory size in MB. 256 MB is plenty — the workload is a single INSERT plus an IAM token mint."
  type        = number
  default     = 256

  validation {
    condition     = var.memory_mb >= 128 && var.memory_mb <= 10240
    error_message = "memory_mb must be between 128 and 10240."
  }
}

variable "timeout_seconds" {
  description = "Lambda timeout. Cognito's Post-Confirmation invocation has a hard 5-second budget but allows the trigger Lambda itself up to ~5 seconds to respond before Cognito retries. We set 10 to give some headroom for VPC cold starts."
  type        = number
  default     = 10

  validation {
    condition     = var.timeout_seconds >= 3 && var.timeout_seconds <= 30
    error_message = "timeout_seconds must be between 3 and 30."
  }
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the Lambda's log group. 30 days is sufficient for the PoC; production should match the org's audit retention policy."
  type        = number
  default     = 30
}

variable "log_level" {
  description = "Python logging level inside the Lambda."
  type        = string
  default     = "INFO"

  validation {
    condition     = contains(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], var.log_level)
    error_message = "log_level must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL."
  }
}

variable "kms_key_arn" {
  description = "Optional CMK ARN used to encrypt the Lambda environment variables. When null, AWS-owned keys are used. Production may want to pass the Aurora module's CMK here to keep encryption keys consistent."
  type        = string
  default     = null
}

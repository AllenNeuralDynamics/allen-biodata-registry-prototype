###############################################################################
# Variables — lambdas/seed-smoke-test module
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
  description = "Absolute path to the Lambda source directory (the directory containing handler.py, smoke_test.py, and requirements.txt). Typically '$${path.module}/../../../../services/seed-smoke-test' from the dev composition."
  type        = string
}

variable "build_dir" {
  description = "Absolute path to a working directory the module owns for staging the deployment package. Anything under this path may be deleted and recreated on every apply. Defaults to a per-module temp directory under the calling Terraform working directory."
  type        = string
  default     = null
}

variable "python_executable" {
  description = "Python executable used to install runtime dependencies into the staging directory. Defaults to 'python3'. The interpreter version should match the Lambda runtime (3.12). pg8000 is pure-Python so any 3.x interpreter works."
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
  description = "DB user the smoke test authenticates as. Defaults to 'migration_runner' — same user the seeder uses, because rds_superuser membership grants BYPASSRLS so the SELECTs return all rows regardless of governance state. Production should split to a read-only smoke_test_runner role once per-table grants stabilise; the IAM policy already scopes to a single {cluster_resource_id, db_user} tuple either way."
  type        = string
  default     = "migration_runner"
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

###############################################################################
# Smoke-test thresholds (env vars)
###############################################################################

variable "min_data_assets" {
  description = "Minimum row count required in `data_asset`. The 10% sample of the customer snapshot produces ~10k rows; the default of 10 is conservatively below that so the test still passes when an operator runs against a smaller sub-sample for development. Production should bump this to a realistic floor (e.g. ~5000) so a partial-seed failure is still caught."
  type        = number
  default     = 10

  validation {
    condition     = var.min_data_assets >= 0
    error_message = "min_data_assets must be non-negative."
  }
}

variable "min_subjects" {
  description = "Minimum row count required in `subject`. Default 1 — the smoke test only confirms at least one subject was seeded, not that every Data_Asset has one (the shared-vs-asset-specific contract permits assets without subjects)."
  type        = number
  default     = 1

  validation {
    condition     = var.min_subjects >= 0
    error_message = "min_subjects must be non-negative."
  }
}

variable "min_instruments" {
  description = "Minimum row count required in `instrument`. Default 1. Same rationale as min_subjects."
  type        = number
  default     = 1

  validation {
    condition     = var.min_instruments >= 0
    error_message = "min_instruments must be non-negative."
  }
}

variable "min_sessions" {
  description = "Minimum row count required in `session`. Default 1. Same rationale as min_subjects."
  type        = number
  default     = 1

  validation {
    condition     = var.min_sessions >= 0
    error_message = "min_sessions must be non-negative."
  }
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
  description = "Lambda memory size in MB. Every check is a single SELECT COUNT/EXISTS — memory pressure is negligible. 256 MB is comfortably above what pg8000 + the smoke-test logic need."
  type        = number
  default     = 256

  validation {
    condition     = var.memory_mb >= 128 && var.memory_mb <= 10240
    error_message = "memory_mb must be between 128 and 10240."
  }
}

variable "timeout_seconds" {
  description = "Lambda timeout. The whole suite finishes in well under a second against a freshly-seeded cluster; 60s is comfortably above that and gives connection establishment + IAM token mint enough headroom even from a cold start."
  type        = number
  default     = 60

  validation {
    condition     = var.timeout_seconds >= 10 && var.timeout_seconds <= 900
    error_message = "timeout_seconds must be between 10 and 900."
  }
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the Lambda's log group. Smoke-test logs are diagnostically valuable for QC1 walkthroughs — 90 days is a reasonable PoC default."
  type        = number
  default     = 90
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
  description = "Optional CMK ARN used to encrypt the Lambda environment variables. When null, AWS-owned keys are used."
  type        = string
  default     = null
}

###############################################################################
# Invocation behavior
###############################################################################

variable "invoke_on_apply" {
  description = "When true, an `aws_lambda_invocation` resource invokes the smoke test synchronously during every `terraform apply` whose source-hash trigger has changed. This is the production path — it means a successful apply guarantees the seeded data is present and consistent. A failed smoke test fails the apply, exactly the contract that prevents 'silent seed failure surfaces as empty OpenSearch at QC1'. Set to false to skip auto-invocation (operators run the Lambda manually, e.g. via `aws lambda invoke`)."
  type        = bool
  default     = true
}

variable "invocation_payload" {
  description = "JSON payload passed to the Lambda when `invoke_on_apply = true`. The handler accepts `min_data_assets`, `min_subjects`, `min_instruments`, `min_sessions` overrides; the default empty object delegates to the env-var defaults."
  type        = string
  default     = "{}"
}

variable "invocation_extra_triggers" {
  description = "Optional map of extra trigger key/value pairs that bump the smoke-test re-invocation hash. The dev composition typically passes `{ seeder_invocation = module.seeder.invocation_result }` so the smoke test re-runs every time the seeder produces a new summary."
  type        = map(string)
  default     = {}
}

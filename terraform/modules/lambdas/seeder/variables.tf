###############################################################################
# Variables — lambdas/seeder module
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
  description = "Absolute path to the Lambda source directory (the directory containing handler.py, seeder.py, mapping.py, and requirements.txt). Typically '$${path.module}/../../../../services/seeder' from the dev composition."
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
  description = "DB user the seeder authenticates as. Defaults to 'migration_runner' — same user the migration-runner Lambda uses, because the seeder is also a bring-up-time tool that needs INSERT privileges on every registry table (data_asset, subject, instrument, rig, procedures, session, acquisition, processing, quality_control, data_description, all four junctions, plus organization/space/app_user for the bootstrap step). Using migration_runner avoids creating a parallel seeder_runner role with effectively the same privileges. Production should split the roles once per-table grants stabilise."
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
# Seed-source configuration (env vars)
###############################################################################

variable "seed_s3_bucket" {
  description = "S3 bucket containing the aind-data-schema snapshot. The PoC default targets the customer-provided sample at 's3://aind-scratch-data/jon.young/metadata_v2_records_20260324/data_assets.json'."
  type        = string
  default     = "aind-scratch-data"
}

variable "seed_s3_key" {
  description = "S3 key of the JSON snapshot to seed. Default points at the 7 GB customer snapshot; sample_fraction controls how much of it is processed."
  type        = string
  default     = "jon.young/metadata_v2_records_20260324/data_assets.json"
}

variable "seed_sample_fraction" {
  description = "Fraction (0.0, 1.0] of records to seed. Selection is deterministic per record content (SHA-256 modulo). Default 0.1 matches the PoC plan's '10% sample'. Set to 1.0 to seed everything (production scale-up should bump memory and timeout first)."
  type        = number
  default     = 0.1

  validation {
    condition     = var.seed_sample_fraction > 0.0 && var.seed_sample_fraction <= 1.0
    error_message = "seed_sample_fraction must be in the range (0.0, 1.0]."
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
  description = "Security group IDs attached to the Lambda's ENIs. The SGs must permit egress to Aurora's security group on port 5432 AND egress to S3 (via interface or gateway VPC endpoint). The dev composition typically reuses the Aurora client SG from `module.vpc`."
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
  description = "Lambda memory size in MB. The seeder loads the entire snapshot file into memory via json.loads, so the floor is set by the file size — the 10% sample (~700 MB) fits in 1024 MB; the full 7 GB snapshot would need a streaming parser (see services/seeder/requirements.txt)."
  type        = number
  default     = 1024

  validation {
    condition     = var.memory_mb >= 512 && var.memory_mb <= 10240
    error_message = "memory_mb must be between 512 and 10240."
  }
}

variable "timeout_seconds" {
  description = "Lambda timeout. The seeder takes 5–15 minutes for the 10% sample (~10k records, each fanning out to 10+ INSERTs across 14 tables). Default is the Lambda hard ceiling (900s / 15 minutes). For PoC volume the single-invocation pattern is fine; if the corpus grows, switch to a chunked SQS-driven pattern before bumping further."
  type        = number
  default     = 900

  validation {
    condition     = var.timeout_seconds >= 60 && var.timeout_seconds <= 900
    error_message = "timeout_seconds must be between 60 and 900 (Lambda's hard ceiling)."
  }
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the Lambda's log group. Seed logs are diagnostically valuable for QC1 walkthroughs — 90 days is a reasonable PoC default."
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
  description = "When true, an `aws_lambda_invocation` resource invokes the seeder synchronously during every `terraform apply` whose source-hash trigger has changed. This is the production path — it means a successful apply guarantees the sample data is loaded. Set to false to skip auto-invocation (operators run the Lambda manually, e.g. via `aws lambda invoke`)."
  type        = bool
  default     = true
}

variable "invocation_payload" {
  description = "JSON payload passed to the Lambda when `invoke_on_apply = true`. The handler accepts `bucket`, `key`, `sample_fraction`, and `max_records` overrides; the default empty object delegates to the env-var defaults."
  type        = string
  default     = "{}"
}

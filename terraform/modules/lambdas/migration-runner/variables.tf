###############################################################################
# Variables — lambdas/migration-runner module
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
  description = "Absolute path to the Lambda source directory (the directory containing handler.py, runner.py, and requirements.txt). Typically '$${path.module}/../../../../services/migration-runner' from the dev composition."
  type        = string
}

variable "migrations_dir" {
  description = "Absolute path to the directory containing the *.sql migration files. Every *.sql file under this directory is copied into the deployment zip at /var/task/migrations/ so the Lambda finds them at runtime. Typically '$${path.module}/../../../../migrations' from the dev composition."
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
  description = "Privileged DB user the migration runner authenticates as. Must (a) exist, (b) have membership in the `rds_iam` Aurora role, and (c) have superuser-equivalent privileges within the target database (CREATE EXTENSION, CREATE TABLE, GRANT, CREATE POLICY, ALTER TABLE … ENABLE ROW LEVEL SECURITY). The Aurora bootstrap is responsible for creating this user; this module only consumes it."
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
  description = "Lambda memory size in MB. 512 MB gives Postgres parsing/execution comfortable headroom; the workload is a sequence of DDL statements, not heavy computation."
  type        = number
  default     = 512

  validation {
    condition     = var.memory_mb >= 128 && var.memory_mb <= 10240
    error_message = "memory_mb must be between 128 and 10240."
  }
}

variable "timeout_seconds" {
  description = "Lambda timeout. Generous default (300s) accommodates first-run migrations on a freshly-provisioned Aurora cluster — typically 30–90s for the seven-migration corpus, plus 15–30s of cold-start + first-connection latency. Bump if the migrations corpus grows past several MB or includes heavy index builds."
  type        = number
  default     = 300

  validation {
    condition     = var.timeout_seconds >= 30 && var.timeout_seconds <= 900
    error_message = "timeout_seconds must be between 30 and 900 (Lambda's hard ceiling)."
  }
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the Lambda's log group. Migration logs are diagnostically valuable but not high-volume — 90 days is a reasonable PoC default. Production should match the org's audit retention policy."
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
  description = "Optional CMK ARN used to encrypt the Lambda environment variables. When null, AWS-owned keys are used. Production may want to pass the Aurora module's CMK here to keep encryption keys consistent."
  type        = string
  default     = null
}

###############################################################################
# Invocation behavior
###############################################################################

variable "invoke_on_apply" {
  description = "When true, a `aws_lambda_invocation` data source invokes the migration runner synchronously during every `terraform apply`. This is the production path — it means a successful apply guarantees the schema is up to date. Set to false to skip auto-invocation (operators run the Lambda manually, e.g. via `aws lambda invoke`)."
  type        = bool
  default     = true
}

variable "invocation_payload" {
  description = "JSON payload passed to the Lambda when `invoke_on_apply = true`. The handler currently accepts `migrations_dir` and `applied_by` overrides; the default empty object delegates to the module's defaults (which is what the production path uses)."
  type        = string
  default     = "{}"
}

###############################################################################
# Variables — lambdas/registration module
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
  description = "Absolute path to the Lambda source directory (the directory containing handler.py). The dev composition typically supplies '$${path.module}/../../../services/registration-lambda'."
  type        = string
}

variable "openapi_spec_path" {
  description = "Optional override for the OpenAPI 3.0 spec path. Defaults to '<source_dir>/../../openapi/openapi.yaml' so the spec is bundled into the deployment image and the Lambda can validate requests without a runtime fetch."
  type        = string
  default     = null
}

variable "build_dir" {
  description = "Absolute path to a working directory the module owns for staging the deployment package. Anything under this path may be deleted and recreated on every apply. Defaults to a per-module temp directory under the calling Terraform working directory."
  type        = string
  default     = null
}

variable "shared_layer_arn" {
  description = "ARN of the shared Lambda Layer (biodata_registry_shared, Task 12.1). Required — this Lambda imports `biodata_registry_shared` for auth-context parsing, RLS-aware Aurora connection, OpenAPI middleware, sensitive-flag enforcement, and error shaping. Without the Layer the function will fail at cold-start with `ImportError: biodata_registry_shared`."
  type        = string

  validation {
    condition     = length(var.shared_layer_arn) > 0
    error_message = "shared_layer_arn is required — the Registration Lambda imports `biodata_registry_shared` from the shared Layer."
  }
}

###############################################################################
# Aurora connection (env vars)
###############################################################################

variable "aurora_host" {
  description = "Aurora writer endpoint hostname. From `module.aurora.cluster_endpoint`."
  type        = string
}

variable "aurora_port" {
  description = "Aurora TCP port (5432 for PostgreSQL)."
  type        = number
  default     = 5432
}

variable "aurora_db_name" {
  description = "Aurora database name. From `module.aurora.db_name`."
  type        = string
}

variable "db_user" {
  description = "Aurora database user the Registration Lambda authenticates as. The user must (a) exist, (b) be granted membership in the `rds_iam` role for IAM database authentication, and (c) have INSERT, UPDATE, SELECT on `data_asset`, `subject`, `instrument`, `rig`, `procedures`, `session`, `acquisition`, `processing`, `quality_control`, `data_description`, plus INSERT, SELECT (NOT UPDATE/DELETE — see migration 0004) on `entity_revision`. Created by the schema migration runner (Task 8.1)."
  type        = string
}

variable "aurora_cluster_resource_id" {
  description = "Immutable Aurora cluster resource id (`cluster-xxx`). Used to scope the IAM policy granting `rds-db:connect` to *this* cluster + DB user, not all clusters in the account. From `module.aurora.cluster_resource_id`."
  type        = string
}

variable "aurora_cluster_arn" {
  description = "Aurora cluster ARN. Documented for completeness — the IAM policy uses the resource id form. From `module.aurora.cluster_arn`."
  type        = string
  default     = null
}

variable "db_sslmode" {
  description = "psycopg sslmode for the Aurora connection. 'require' is the minimum for Aurora; production may upgrade to 'verify-full' once the AmazonRootCA bundle is bundled into the Layer."
  type        = string
  default     = "require"

  validation {
    condition     = contains(["disable", "allow", "prefer", "require", "verify-ca", "verify-full"], var.db_sslmode)
    error_message = "db_sslmode must be one of disable, allow, prefer, require, verify-ca, verify-full."
  }
}

variable "db_connect_timeout_seconds" {
  description = "TCP/TLS handshake timeout. Registration is on the hot path of every write; we keep this short so a network hiccup does not stall the API Gateway request beyond its integration timeout."
  type        = number
  default     = 10
}

variable "db_statement_timeout_ms" {
  description = "Per-statement timeout (milliseconds) issued via `SET LOCAL statement_timeout` by the shared connection helper. 10s is plenty for CRUD; raise for bulk-import paths."
  type        = number
  default     = 10000
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
  description = "Security group IDs attached to the Lambda's ENIs. The SGs must permit egress to Aurora's security group on port 5432. The dev composition typically reuses the internal/Aurora-client SG from `module.vpc`."
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
  description = "Lambda memory size in MB. 1024 MB is a balanced default — it provides ~2 vCPU equivalents which speeds psycopg connection setup + JSONB encoding compared to the 256/512 starter sizes."
  type        = number
  default     = 1024

  validation {
    condition     = var.memory_mb >= 128 && var.memory_mb <= 10240
    error_message = "memory_mb must be between 128 and 10240."
  }
}

variable "timeout_seconds" {
  description = "Lambda timeout. API Gateway has a 30s integration timeout, so any value beyond that is wasted; we set 25 to give the integration a 5s response buffer."
  type        = number
  default     = 25

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
  description = "Optional CMK ARN used to encrypt the Lambda environment variables. When null, AWS-owned keys are used."
  type        = string
  default     = null
}

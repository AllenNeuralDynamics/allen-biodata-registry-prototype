variable "name_prefix" {
  type    = string
  default = "biodata-registry-dev"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "project" {
  type    = string
  default = "biodata-registry"
}

variable "region" {
  type    = string
  default = "us-west-2"
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "source_dir" {
  type = string
}

variable "build_dir" {
  type    = string
  default = null
}

variable "python_executable" {
  type    = string
  default = "python3"
}

variable "opensearch_endpoint" {
  type = string
}

variable "opensearch_collection_arn" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
}

variable "security_group_ids" {
  type = list(string)
}

variable "memory_mb" {
  type    = number
  default = 1024
}

variable "timeout_seconds" {
  type    = number
  default = 30
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "log_level" {
  type    = string
  default = "INFO"
}

# ------------------------------------------------------------------------- #
# NL search (POST /search/nl) — optional. When all four nl_* variables are
# set, the Lambda's IAM role gains bedrock + KB read access and the
# Aurora env vars are populated for RLS-aware SQL execution.
# ------------------------------------------------------------------------- #

variable "bedrock_kb_id" {
  description = "Bedrock Knowledge Base ID consumed by POST /search/nl. Empty string disables NL search."
  type        = string
  default     = ""
}

variable "nl_model_id" {
  description = "Bedrock model identifier used for NL→SQL (defaults to Claude Opus 4.7 inference profile)."
  type        = string
  default     = "us.anthropic.claude-opus-4-7"
}

variable "aurora_host" {
  type    = string
  default = ""
}

variable "aurora_port" {
  type    = number
  default = 5432
}

variable "aurora_db_name" {
  type    = string
  default = ""
}

variable "aurora_db_user" {
  type    = string
  default = "biodata_app"
}

variable "aurora_cluster_resource_id" {
  type    = string
  default = ""
}

variable "redis_primary_endpoint" {
  type    = string
  default = ""
}

variable "redis_auth_token_secret_arn" {
  description = "Secrets Manager secret ARN for the Redis auth token. The Lambda fetches the token at cold start; left empty when Redis is not configured."
  type        = string
  default     = ""
}

variable "aws_account_id" {
  type    = string
  default = ""
}

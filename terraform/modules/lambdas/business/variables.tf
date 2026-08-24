variable "name_prefix" {
  type    = string
  default = "biodata-registry-dev"
}

variable "function_suffix" {
  description = "Suffix appended to name_prefix to form the function name. E.g. 'validation' -> 'biodata-registry-dev-validation'."
  type        = string
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

variable "shared_layer_arn" {
  type    = string
  default = null
}

# Aurora connection (env vars, IAM auth)
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

variable "db_user" {
  type    = string
  default = "biodata_app"
}

variable "aurora_cluster_resource_id" {
  type    = string
  default = ""
}

variable "enable_rds_iam_auth" {
  type    = bool
  default = true
}

# Extra IAM statements (Bedrock, OpenSearch, Scheduler, SNS, etc.)
variable "extra_iam_statements" {
  type    = list(any)
  default = []
}

# Extra environment variables
variable "extra_environment" {
  type    = map(string)
  default = {}
}

# VPC
variable "subnet_ids" {
  type = list(string)
}

variable "security_group_ids" {
  type = list(string)
}

# Sizing
variable "memory_mb" {
  type    = number
  default = 1024
}

variable "timeout_seconds" {
  type    = number
  default = 25
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "log_level" {
  type    = string
  default = "INFO"
}

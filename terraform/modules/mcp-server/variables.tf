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

variable "python_executable" {
  type    = string
  default = "python3"
}

# Aurora — for tools that query directly (get_validation_status,
# explore_schema).
variable "aurora_host" {
  type = string
}

variable "aurora_port" {
  type    = number
  default = 5432
}

variable "aurora_db_name" {
  type = string
}

variable "db_user" {
  type    = string
  default = "biodata_app"
}

variable "aurora_cluster_resource_id" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
}

variable "security_group_ids" {
  type = list(string)
}

# Read-only Lambda function names + ARNs that the MCP server proxies to.
variable "search_lambda_name" { type = string }
variable "search_lambda_arn"  { type = string }

variable "registration_lambda_name" { type = string }
variable "registration_lambda_arn"  { type = string }

variable "collections_lambda_name" { type = string }
variable "collections_lambda_arn"  { type = string }

variable "validation_lambda_name" { type = string }
variable "validation_lambda_arn"  { type = string }

variable "observability_lambda_name" { type = string }
variable "observability_lambda_arn"  { type = string }

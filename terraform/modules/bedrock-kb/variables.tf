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

variable "seed_dir" {
  description = "Local directory whose contents are synced to the KB's S3 bucket as the data source."
  type        = string
}

variable "operator_arn" {
  description = "Optional IAM principal ARN to grant data-access on the KB collection (so an operator can debug the index). Empty string disables."
  type        = string
  default     = ""
}

variable "force_destroy" {
  description = "Whether the KB S3 bucket can be destroyed even if it contains objects (useful for the dev environment)."
  type        = bool
  default     = true
}

###############################################################################
# Variables — Terraform bootstrap
###############################################################################

variable "aws_region" {
  description = "AWS region for the remote-state backend resources."
  type        = string
  default     = "us-west-2"
}

variable "project" {
  description = "Project tag applied to every bootstrap resource."
  type        = string
  default     = "allen-biodata-registry-poc"
}

variable "tags" {
  description = "Additional tags merged onto every bootstrap resource."
  type        = map(string)
  default     = {}
}

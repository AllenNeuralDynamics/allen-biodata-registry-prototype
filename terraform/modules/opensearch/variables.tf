###############################################################################
# Variables — opensearch module
#
# OpenSearch Serverless collection for the Allen BioData Registry.
#
# Index templates and the synonym file live under templates/ in this module.
# The synonym file is uploaded to S3 by this module so it can be referenced
# from the index analyzer; the index templates themselves cannot be created
# by Terraform (the AWS provider does not expose an aoss index resource) —
# that step is a post-apply Lambda or `null_resource` invocation, deferred
# to Task 10's environment composition. See README "Index template
# provisioning is deferred" for details.
#
# Validates: R17.2, R17.3, R17.5, R17.6, R31.3, R32.2.
###############################################################################

variable "name_prefix" {
  description = "Prefix applied to every resource name. Typically '<project>-<environment>', e.g. 'biodata-registry-dev'. The collection is named '<name_prefix>-biodata' and the synonyms bucket is '<name_prefix>-opensearch-config-<account_id>'."
  type        = string
  default     = "biodata-registry-dev"

  validation {
    # Serverless collection names are capped at 32 characters and the
    # collection-name format is "<name_prefix>-biodata" (8 chars suffix).
    condition     = length(var.name_prefix) > 0 && length(var.name_prefix) <= 24
    error_message = "name_prefix must be 1–24 characters (Serverless collection names cap at 32 and we append '-biodata')."
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

variable "vpc_id" {
  description = "VPC ID. The OpenSearch Serverless VPC endpoint is attached here so private-subnet Lambdas can reach the collection without traversing the NAT. From the vpc module's vpc_id output."
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnet IDs the OpenSearch Serverless VPC endpoint is attached to. From the vpc module's private_subnet_ids output. Must be ≥1; OpenSearch Serverless VPC endpoints can attach across multiple AZs for HA."
  type        = list(string)

  validation {
    condition     = length(var.private_subnet_ids) >= 1
    error_message = "Provide at least one private subnet id."
  }
}

variable "security_group_ids" {
  description = "Security group IDs attached to the OpenSearch Serverless VPC endpoint. Typically [vpc.internal_security_group_id] so any resource in the internal SG can reach the collection."
  type        = list(string)

  validation {
    condition     = length(var.security_group_ids) >= 1
    error_message = "Provide at least one security group id (typically the internal SG from the vpc module)."
  }
}

variable "principal_arns" {
  description = "IAM principal ARNs (Indexing_Lambda, Search_Lambda, and Embedding_Backfill_Lambda execution roles) granted index/document read+write actions on the collection. Empty list during initial provisioning is fine — the data access policy can be updated in place once the Lambda modules exist (Tasks 18.1, 19.1, 28.1)."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for arn in var.principal_arns : can(regex("^arn:aws:iam::[0-9]{12}:", arn))
    ])
    error_message = "Every entry in principal_arns must be a fully-qualified IAM ARN (arn:aws:iam::ACCOUNT:role/...)."
  }
}

variable "standby_replicas" {
  description = "Standby replicas for the collection. ENABLED is the SEARCH-type default and is recommended (it doubles the OCU floor but provides HA). DISABLED halves the floor (~$350/mo vs ~$700/mo) and is acceptable for a single-AZ PoC."
  type        = string
  default     = "ENABLED"

  validation {
    condition     = contains(["ENABLED", "DISABLED"], var.standby_replicas)
    error_message = "standby_replicas must be 'ENABLED' or 'DISABLED'."
  }
}

variable "kms_deletion_window_in_days" {
  description = "Pending-delete window for the KMS CMK provisioned by this module. 7 (PoC) — 30 (production). Must be 7–30 per AWS."
  type        = number
  default     = 7

  validation {
    condition     = var.kms_deletion_window_in_days >= 7 && var.kms_deletion_window_in_days <= 30
    error_message = "kms_deletion_window_in_days must be between 7 and 30."
  }
}

variable "synonyms_bucket_force_destroy" {
  description = "If true (PoC default), `terraform destroy` will delete the synonyms S3 bucket even if it still contains objects. Set to false in production so the bucket protects historical synonym lists."
  type        = bool
  default     = true
}

variable "tags" {
  description = "Additional tags merged onto every resource. Project / Environment / Module are added automatically."
  type        = map(string)
  default     = {}
}

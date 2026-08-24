###############################################################################
# Variables — dev environment composition
#
# Inputs to the dev environment composition. Defaults are tuned to the
# Allen Institute account (014097726564, us-west-2). Most values exist so
# the composition can also be reused for a staging environment by passing
# `-var environment=staging`.
###############################################################################

variable "aws_region" {
  description = "AWS region for the regional providers and every regional resource. CloudFront ACM certs always go to us-east-1 regardless (handled by the aliased aws.us_east_1 provider in main.tf)."
  type        = string
  default     = "us-west-2"

  validation {
    condition     = can(regex("^[a-z]{2}-[a-z]+-[0-9]+$", var.aws_region))
    error_message = "aws_region must be a valid AWS region code (e.g. us-west-2)."
  }
}

variable "account_id" {
  description = "Expected AWS account id. Used as a guardrail by the precondition in main.tf — terraform plan/apply fails if the resolved caller identity does not match. Default 014097726564 (Allen Institute biodata-registry sandbox)."
  type        = string
  default     = "014097726564"

  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id must be a 12-digit AWS account id."
  }
}

variable "project" {
  description = "Project tag applied to every resource."
  type        = string
  default     = "biodata-registry"
}

variable "environment" {
  description = "Environment name. Drives the name_prefix passed to every module (`<project>-<environment>`) and the remote-state key prefix (`envs/<environment>/...`)."
  type        = string
  default     = "dev"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,15}$", var.environment))
    error_message = "environment must be 1-16 chars, lowercase letters/digits/hyphens only."
  }
}

variable "tags" {
  description = "Additional tags merged onto every resource on top of the per-module tags."
  type        = map(string)
  default     = {}
}

###############################################################################
# Network sizing — VPC
###############################################################################

variable "vpc_cidr" {
  description = "Primary IPv4 CIDR for the VPC. Default 10.40.0.0/16 leaves room for 3 private /20 + 3 public /24 subnets."
  type        = string
  default     = "10.40.0.0/16"
}

variable "availability_zones" {
  description = "AZs the VPC spans. Aurora and DocumentDB both require ≥2 AZs; the default 3-AZ layout (us-west-2a/b/c) gives the data plane HA headroom even though the PoC runs single-instance for cost."
  type        = list(string)
  default     = ["us-west-2a", "us-west-2b", "us-west-2c"]
}

variable "single_nat_gateway" {
  description = "If true (PoC default), provision exactly one NAT gateway. Saves ~$64/mo vs the 3-NAT HA layout. Set false in production."
  type        = bool
  default     = true
}

###############################################################################
# Aurora sizing
###############################################################################

variable "aurora_engine_version" {
  description = "Aurora PostgreSQL engine version. 16.13 is the latest stable 16.x with pgvector."
  type        = string
  default     = "16.13"
}

variable "aurora_min_capacity_acu" {
  description = "Aurora Serverless v2 minimum ACU (1 ACU ≈ 2 GiB). 0.5 floor keeps idle cost near $43/mo."
  type        = number
  default     = 0.5
}

variable "aurora_max_capacity_acu" {
  description = "Aurora Serverless v2 maximum ACU. 4.0 is plenty for PoC traffic."
  type        = number
  default     = 4.0
}

variable "aurora_instance_count" {
  description = "Number of Aurora Serverless v2 instances. PoC default 1 (writer only, no reader replicas)."
  type        = number
  default     = 1
}

###############################################################################
# DocumentDB sizing
###############################################################################

variable "documentdb_instance_class" {
  description = "DocumentDB instance class. db.r6g.large is the smallest DocumentDB 5.0 production class (~$210/mo)."
  type        = string
  default     = "db.r6g.large"
}

variable "documentdb_instance_count" {
  description = "Number of DocumentDB cluster instances. PoC default 1 (no failover HA)."
  type        = number
  default     = 1
}

variable "documentdb_enable_readonly_user_bootstrap" {
  description = "If true, the documentdb module runs `mongosh` post-apply to create the `biodata_reader` user. Disable when running terraform apply from outside the VPC — the bootstrap requires VPC reach to the cluster on port 27017."
  type        = bool
  default     = false
}

###############################################################################
# OpenSearch sizing
###############################################################################

variable "opensearch_standby_replicas" {
  description = "OpenSearch Serverless standby replicas. ENABLED is the recommended HA default (~$700/mo floor); DISABLED halves it (~$350/mo) and is the PoC default."
  type        = string
  default     = "DISABLED"

  validation {
    condition     = contains(["ENABLED", "DISABLED"], var.opensearch_standby_replicas)
    error_message = "opensearch_standby_replicas must be ENABLED or DISABLED."
  }
}

###############################################################################
# ElastiCache sizing
###############################################################################

variable "elasticache_node_type" {
  description = "ElastiCache Redis node type. cache.t4g.micro is the cheapest Graviton2 option (~$13/mo per node) and adequate for PoC working-set sizes."
  type        = string
  default     = "cache.t4g.micro"
}

variable "elasticache_num_cache_clusters" {
  description = "Number of cache clusters in the replication group. 2 = 1 primary + 1 replica with automatic failover (the PoC default for Multi-AZ HA)."
  type        = number
  default     = 2
}

###############################################################################
# Cognito
###############################################################################

variable "cognito_saml_metadata_url" {
  description = "URL or inline XML of the Allen Institute SAML IdP metadata. When null (PoC default), SAML federation is skipped and the User Pool only accepts Cognito-hosted username/password authentication. The customer can flip this on later without other code changes."
  type        = string
  default     = null
}

variable "cognito_extra_callback_urls" {
  description = "Additional OAuth callback URLs to append to the Cognito User Pool client. The composition automatically appends the CloudFront distribution domain; this variable is for any additional URLs (e.g. a staging hostname or a custom domain)."
  type        = list(string)
  default     = []
}

variable "cognito_extra_logout_urls" {
  description = "Additional OAuth sign-out URLs to append to the Cognito User Pool client (alongside the auto-appended CloudFront domain)."
  type        = list(string)
  default     = []
}

###############################################################################
# CloudFront / Web App
###############################################################################

variable "enable_custom_domain" {
  description = "When true, the cloudfront-s3 module provisions an ACM certificate in us-east-1 for var.custom_domain and binds it to the distribution. The customer must add the ACM validation CNAME records (printed by `terraform output acm_validation_records`) to their DNS zone before apply completes — otherwise apply hangs on validation. Default false: the distribution uses the CloudFront-provided default cert at *.cloudfront.net."
  type        = bool
  default     = false
}

variable "custom_domain" {
  description = "Custom domain name for the CloudFront distribution (e.g. registry.alleninstitute.org). Only consulted when enable_custom_domain = true."
  type        = string
  default     = null

  validation {
    condition     = var.custom_domain == null || can(regex("^[a-z0-9][a-z0-9.-]*[a-z0-9]$", var.custom_domain))
    error_message = "custom_domain must be a lowercase DNS-style hostname or null."
  }
}

variable "enable_cloudfront_logging" {
  description = "When true, the cloudfront-s3 module provisions a separate access-log bucket and configures CloudFront to write standard logs there. PoC default false to keep costs minimal."
  type        = bool
  default     = false
}

###############################################################################
# Lambda packaging
###############################################################################

variable "python_executable" {
  description = "Python interpreter used to install runtime dependencies into Lambda deployment packages. Should match the Lambda runtime (python3.12)."
  type        = string
  default     = "python3"
}

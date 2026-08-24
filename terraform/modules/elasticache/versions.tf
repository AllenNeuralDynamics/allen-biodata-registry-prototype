###############################################################################
# Allen BioData Registry PoC — elasticache module
#
# Pinned versions matching the rest of the stack. The dev environment
# composition (terraform/envs/dev) is responsible for declaring the providers;
# this module only states the constraints.
#
# `random` is required by the AUTH-token generator that feeds the Redis
# replication group's `auth_token` and the Secrets Manager secret.
###############################################################################

terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.5"
    }
  }
}

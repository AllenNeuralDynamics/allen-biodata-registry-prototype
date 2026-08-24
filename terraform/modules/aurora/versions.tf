###############################################################################
# Allen BioData Registry PoC — aurora module
#
# Pinned versions matching the rest of the stack. The dev environment
# composition (terraform/envs/dev) is responsible for declaring the providers;
# this module only states the constraints.
#
# `random` is required by the master-password generator that feeds the
# Secrets Manager secret.
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
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2"
    }
  }
}

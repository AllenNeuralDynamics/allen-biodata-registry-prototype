###############################################################################
# Allen BioData Registry PoC — lambdas/indexing module
#
# Pinned versions matching the rest of the stack. The dev environment
# composition (terraform/envs/dev) is responsible for declaring the AWS
# provider; this module only states the constraints.
###############################################################################

terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2"
    }
  }
}

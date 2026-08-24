###############################################################################
# Allen BioData Registry PoC — opensearch module
#
# Pinned to match the rest of the stack. The dev environment composition
# (terraform/envs/dev) declares the AWS provider; this module only states
# the constraint.
###############################################################################

terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

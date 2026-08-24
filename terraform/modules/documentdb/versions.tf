###############################################################################
# Allen BioData Registry PoC — documentdb module
#
# Pinned to match the rest of the stack. The dev environment composition
# (terraform/envs/dev) declares the AWS provider; this module only states
# the constraints.
#
#   * `random` is used by the master + read-only password generators.
#   * `null` powers the post-apply mongosh bootstrap that creates the
#     biodata_reader DB user.
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

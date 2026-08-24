###############################################################################
# Allen BioData Registry PoC — cdc-pipeline module
#
# Pinned versions matching the rest of the stack. The dev environment
# composition (terraform/envs/dev) is responsible for declaring the
# providers; this module only states the constraints.
#
# `archive` is required because the CDC Reader Lambda is packaged here
# (a placeholder zip until services/cdc-reader/ lands as part of Task
# 18.x — see README "Implementation Gap" section for the full story).
# `null` is used by the placeholder source builder so a fresh checkout
# can `terraform validate` even before the Python source tree exists.
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

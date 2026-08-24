###############################################################################
# Allen BioData Registry PoC — Terraform Bootstrap
#
# Pinned versions for the one-time bootstrap that provisions the remote state
# backend (S3 + DynamoDB + KMS) used by every downstream Terraform module in
# this repo.
#
# This config intentionally uses LOCAL state. See main.tf for rationale.
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

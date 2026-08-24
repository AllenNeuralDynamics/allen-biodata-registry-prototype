###############################################################################
# Validation wrapper for the cloudfront-s3 module
#
# This file exists ONLY so `terraform validate` can resolve the module's
# `configuration_aliases = [aws.us_east_1]`. Modules that declare aliased
# providers cannot validate standalone — they need a composition that
# supplies the aliased provider configuration.
#
# Usage:
#   terraform -chdir=_validate init -backend=false
#   terraform -chdir=_validate validate
#
# This wrapper is NEVER applied. It does not import to a real state file
# and does not target a real AWS account.
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

provider "aws" {
  region                      = "us-west-2"
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
  access_key                  = "mock_access_key"
  secret_key                  = "mock_secret_key"
}

provider "aws" {
  alias                       = "us_east_1"
  region                      = "us-east-1"
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
  access_key                  = "mock_access_key"
  secret_key                  = "mock_secret_key"
}

###############################################################################
# Three instantiations to exercise every branch of the parent module:
#   1. default               — no logging, no custom domain (PoC default).
#   2. with_logs             — enable_logging = true.
#   3. with_custom_domain    — var.custom_domain set, exercising the
#                              conditional ACM cert + validation resources.
#
# Note: `name_prefix` collisions across these stanzas are harmless because
# `terraform validate` does not actually create any AWS resources — it
# only type-checks the configuration. The validation wrapper never runs
# `apply`.
###############################################################################

module "cloudfront_s3_default" {
  source = "./.."

  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  name_prefix = "biodata-registry-dev"
  environment = "dev"
  project     = "biodata-registry"
}

module "cloudfront_s3_with_logs" {
  source = "./.."

  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  name_prefix    = "biodata-registry-dev"
  environment    = "dev"
  project        = "biodata-registry"
  enable_logging = true
}

module "cloudfront_s3_with_custom_domain" {
  source = "./.."

  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  name_prefix   = "biodata-registry-dev"
  environment   = "dev"
  project       = "biodata-registry"
  custom_domain = "registry.example.org"
}

###############################################################################
# Allen BioData Registry PoC — cloudfront-s3 module
#
# Pinned to match the rest of the stack. The dev environment composition
# (terraform/envs/dev) declares the AWS providers; this module only states
# the constraints AND declares its required `configuration_aliases` because
# CloudFront ACM certificates MUST live in us-east-1 regardless of where the
# rest of the stack runs.
#
# Two AWS providers are required by this module:
#   * aws         — default; manages the S3 bucket, CloudFront distribution,
#                   bucket policy, KMS key, response headers policy, OAC,
#                   and (optionally) the access-log bucket. Runs in the
#                   stack's primary region (typically us-west-2).
#   * aws.us_east_1 — alias; manages the ACM certificate when var.custom_domain
#                     is non-null. CloudFront only accepts ACM certificates
#                     from us-east-1, regardless of where the rest of the
#                     stack runs. The consuming composition declares this
#                     aliased provider:
#                       provider "aws" {
#                         alias  = "us_east_1"
#                         region = "us-east-1"
#                       }
#                     and passes it explicitly:
#                       module "cloudfront_s3" {
#                         providers = {
#                           aws           = aws
#                           aws.us_east_1 = aws.us_east_1
#                         }
#                         ...
#                       }
###############################################################################

terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source                = "hashicorp/aws"
      version               = "~> 5.0"
      configuration_aliases = [aws.us_east_1]
    }
  }
}

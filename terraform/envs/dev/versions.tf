###############################################################################
# Allen BioData Registry PoC — dev environment composition
#
# Pinned versions matching every downstream module. The composition is the
# authoritative declaration of provider versions; modules only declare
# constraints (`required_providers` with no `version` config).
#
# Provider matrix:
#   * aws            — default; manages every regional resource (VPC, Aurora,
#                      DocumentDB, OpenSearch Serverless, ElastiCache,
#                      Cognito, Lambdas, S3, KMS, Secrets Manager). Region
#                      is supplied via var.aws_region (default us-west-2).
#   * aws.us_east_1  — alias; required by the cloudfront-s3 module to
#                      provision the ACM certificate for an optional custom
#                      domain. CloudFront only accepts ACM certs from
#                      us-east-1.
#   * archive        — required by the post-confirmation, migration-runner,
#                      and (future) seeder Lambda modules to zip handler
#                      sources before upload.
#   * null           — required by the aurora module's logical-replication-
#                      slot bootstrap and the Lambda packaging modules.
#   * random         — required by the aurora module's master-password
#                      generator.
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
    random = {
      source  = "hashicorp/random"
      version = "~> 3.5"
    }
  }
}

###############################################################################
# Allen BioData Registry PoC — opensearch module
#
# Provisions:
#   * Customer-managed KMS CMK + alias for at-rest encryption (R31.3).
#   * S3 bucket + uploaded synonyms file. The bucket is named
#     "<name_prefix>-opensearch-config-<account_id>"; the file is
#     "biodata_synonyms.txt". The post-apply index-template provisioning
#     step (Task 10) downloads the file and inlines its contents into the
#     synonym filter at index-create time (R17.3).
#   * OpenSearch Serverless collection of type SEARCH (lexical + KNN
#     hybrid; not VECTORSEARCH, which would lose the inverted-index features
#     Search_Lambda needs for BM25 + synonyms + facets).
#   * Encryption, network, and data-access security policies.
#   * VPC interface endpoint so private-subnet Lambdas reach the collection
#     without traversing NAT (R31.3, R32.2).
#
# What is NOT in this module (deferred to Task 10):
#   * Creation of the data_asset / subject / instrument indices themselves.
#     The AWS provider does not expose an aoss index resource. Templates
#     live under templates/ and are uploaded by a post-apply Lambda or
#     `null_resource` + `local-exec` running curl/python with SigV4 auth.
#
# Validates: R17.2 (per-field boost configuration is applied in Search_Lambda's
# query DSL — design choice), R17.3 (synonym file in S3, referenced by the
# index template), R17.5 (knn_vector(1024) for description_vec), R17.6
# (search_as_you_type for name_suggest), R31.3 (KMS CMK + VPC-only),
# R32.2 (terraform apply provisions OpenSearch).
###############################################################################

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id      = data.aws_caller_identity.current.account_id
  region          = data.aws_region.current.name
  collection_name = "${var.name_prefix}-biodata"

  # The synonyms bucket is per-account / per-region to avoid cross-account
  # name collisions. Bucket names are 3–63 chars, lowercase, no underscores.
  synonyms_bucket_name = "${var.name_prefix}-opensearch-config-${local.account_id}"

  common_tags = merge(
    {
      Project     = var.project
      Environment = var.environment
      Module      = "opensearch"
      ManagedBy   = "terraform"
    },
    var.tags,
  )

  # Indices the post-apply bootstrap step creates. Listed for output
  # convenience so callers can wire the templates straight into the
  # provisioning script.
  index_names = ["data_asset", "subject", "instrument"]

  index_template_paths = {
    data_asset = "${path.module}/templates/data_asset_index_template.json"
    subject    = "${path.module}/templates/subject_index_template.json"
    instrument = "${path.module}/templates/instrument_index_template.json"
  }

  synonyms_local_file_path = "${path.module}/templates/biodata_synonyms.txt"
  synonyms_object_key      = "biodata_synonyms.txt"
}

###############################################################################
# KMS CMK + alias — at-rest encryption for the collection (R31.3).
#
# Key rotation is enabled. The default key policy grants the account root
# full access; downstream IAM consumers (Indexing_Lambda, Search_Lambda)
# get scoped Decrypt access via key grants attached when their modules add
# their execution-role ARNs to var.principal_arns.
###############################################################################

resource "aws_kms_key" "this" {
  description             = "CMK for ${local.collection_name} OpenSearch Serverless collection at-rest encryption (R31.3)."
  enable_key_rotation     = true
  deletion_window_in_days = var.kms_deletion_window_in_days

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-opensearch-kms"
  })
}

resource "aws_kms_alias" "this" {
  name          = "alias/${var.name_prefix}-opensearch"
  target_key_id = aws_kms_key.this.key_id
}

###############################################################################
# Synonyms S3 bucket + object (R17.3).
#
# OpenSearch reads the synonym file at index-create time. The post-apply
# index-template provisioning step (Task 10) downloads the file from this
# bucket and either:
#   (a) inlines its contents into the index template's synonym filter
#       under "synonyms", or
#   (b) uploads the file to the OpenSearch Serverless package store and
#       points the synonym filter at `synonyms_path`.
# Both modes work; mode (a) is simpler for the PoC.
###############################################################################

resource "aws_s3_bucket" "synonyms" {
  bucket        = local.synonyms_bucket_name
  force_destroy = var.synonyms_bucket_force_destroy

  tags = merge(local.common_tags, {
    Name    = local.synonyms_bucket_name
    Purpose = "OpenSearch synonym + index-template config storage"
  })
}

resource "aws_s3_bucket_versioning" "synonyms" {
  bucket = aws_s3_bucket.synonyms.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "synonyms" {
  bucket = aws_s3_bucket.synonyms.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.this.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "synonyms" {
  bucket                  = aws_s3_bucket.synonyms.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_object" "synonyms" {
  bucket       = aws_s3_bucket.synonyms.id
  key          = local.synonyms_object_key
  source       = local.synonyms_local_file_path
  source_hash  = filemd5(local.synonyms_local_file_path)
  content_type = "text/plain"

  # KMS-encrypt at object level too. The bucket-default also covers this,
  # but being explicit avoids any confusion if the bucket default changes.
  # `etag` cannot be used alongside kms_key_id (mutually exclusive in the
  # provider), so source_hash is the change-detection mechanism.
  server_side_encryption = "aws:kms"
  kms_key_id             = aws_kms_key.this.arn

  tags = local.common_tags
}

###############################################################################
# Encryption policy — KMS-encrypt the collection at rest with the CMK above.
#
# OpenSearch Serverless requires an encryption policy to exist BEFORE the
# collection is created.
###############################################################################

resource "aws_opensearchserverless_security_policy" "encryption" {
  name = "${substr(local.collection_name, 0, 24)}-enc"
  type = "encryption"

  policy = jsonencode({
    Rules = [
      {
        ResourceType = "collection"
        Resource     = ["collection/${local.collection_name}"]
      }
    ]
    AWSOwnedKey = false
    KmsARN      = aws_kms_key.this.arn
  })

  description = "KMS-encrypted at rest with customer-managed key (R31.3)."
}

###############################################################################
# VPC endpoint — created first so the network policy can reference it.
#
# OpenSearch Serverless attaches the endpoint to the supplied private
# subnets and the supplied security group. This is the only ingress path
# to the collection — public access is denied by the network policy.
###############################################################################

resource "aws_opensearchserverless_vpc_endpoint" "this" {
  # AWS hard-caps VPC endpoint names at 32 chars. With the "-vpce" suffix
  # (5 chars) reserved, the prefix can be at most 27.
  name               = "${substr(local.collection_name, 0, 27)}-vpce"
  vpc_id             = var.vpc_id
  subnet_ids         = var.private_subnet_ids
  security_group_ids = var.security_group_ids
}

###############################################################################
# Network policy — VPC-only access for both the collection (data plane) and
# Dashboards UI. Public access is disabled (R31.3).
###############################################################################

resource "aws_opensearchserverless_security_policy" "network" {
  name = "${substr(local.collection_name, 0, 24)}-net"
  type = "network"

  policy = jsonencode([
    {
      Description = "Collection (data plane) — VPC-only via VPC endpoint."
      Rules = [
        {
          ResourceType = "collection"
          Resource     = ["collection/${local.collection_name}"]
        }
      ]
      AllowFromPublic = false
      SourceVPCEs     = [aws_opensearchserverless_vpc_endpoint.this.id]
    },
    {
      Description = "Dashboards (UI) — VPC-only via VPC endpoint."
      Rules = [
        {
          ResourceType = "dashboard"
          Resource     = ["collection/${local.collection_name}"]
        }
      ]
      AllowFromPublic = false
      SourceVPCEs     = [aws_opensearchserverless_vpc_endpoint.this.id]
    },
  ])

  description = "VPC-only collection + dashboard access (R31.3)."
}

###############################################################################
# Collection
#
# Type = SEARCH gives us BM25 + inverted-index features (synonyms, facets,
# search_as_you_type) AND knn_vector fields for hybrid lexical+vector search.
# VECTORSEARCH would optimize for pure vector workloads at the cost of the
# lexical features Search_Lambda needs (design.md §Components.Search_Lambda).
###############################################################################

resource "aws_opensearchserverless_collection" "this" {
  name             = local.collection_name
  type             = "SEARCH"
  standby_replicas = var.standby_replicas
  description      = "Allen BioData Registry discovery search (R17). Lexical + faceted + hybrid semantic via knn_vector(1024)."

  tags = merge(local.common_tags, {
    Name = local.collection_name
  })

  # Order matters: encryption + network policies and the VPC endpoint they
  # reference must exist first.
  depends_on = [
    aws_opensearchserverless_security_policy.encryption,
    aws_opensearchserverless_security_policy.network,
    aws_opensearchserverless_vpc_endpoint.this,
  ]
}

###############################################################################
# Data access policy
#
# Grants principals listed in var.principal_arns the index-management +
# document-read/write operations needed by Indexing_Lambda (Task 18.1),
# Embedding_Backfill_Lambda (Task 19.1), and Search_Lambda (Task 28.1), plus
# collection-level CreateCollectionItems / DescribeCollectionItems for the
# bootstrap script that runs the index-template provisioning step.
#
# When principal_arns is empty (initial provisioning before the Lambda
# modules exist), the policy is created with an empty Principal list. The
# policy can be updated in place once the execution roles are known — no
# replacement needed.
###############################################################################

resource "aws_opensearchserverless_access_policy" "data" {
  # OpenSearch Serverless requires Principal to have at least one entry.
  # Skip creation entirely when no principals are wired (initial bootstrap
  # before consumer Lambdas exist); the composition adds the policy in a
  # later apply once Lambda execution roles are known.
  count = length(var.principal_arns) > 0 ? 1 : 0

  name = "${substr(local.collection_name, 0, 24)}-data"
  type = "data"

  policy = jsonencode([
    {
      Description = "Indexing_Lambda + Embedding_Backfill_Lambda + Search_Lambda + bootstrap principals."
      Rules = [
        {
          ResourceType = "index"
          Resource     = ["index/${local.collection_name}/*"]
          Permission = [
            "aoss:CreateIndex",
            "aoss:UpdateIndex",
            "aoss:DeleteIndex",
            "aoss:DescribeIndex",
            "aoss:ReadDocument",
            "aoss:WriteDocument",
          ]
        },
        {
          ResourceType = "collection"
          Resource     = ["collection/${local.collection_name}"]
          Permission = [
            "aoss:CreateCollectionItems",
            "aoss:DescribeCollectionItems",
            "aoss:UpdateCollectionItems",
          ]
        },
      ]
      Principal = var.principal_arns
    },
  ])

  description = "Lambda execution-role + bootstrap-principal data-access policy."
}

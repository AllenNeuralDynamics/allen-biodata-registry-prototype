###############################################################################
# Allen BioData Registry PoC — Terraform Bootstrap
#
# This config provisions the *remote state backend* used by every downstream
# Terraform module in this repo:
#
#   - KMS CMK for SSE on the state bucket
#   - S3 bucket   biodata-registry-tf-state-{account_id}-{region}
#         (versioning + KMS SSE + block public access + 90-day NCV lifecycle)
#   - DynamoDB    biodata-registry-tf-locks
#         (PAY_PER_REQUEST, hash_key = "LockID")
#   - IAM policy granting scoped (non-wildcard) read/write to the bucket,
#     lock table, and KMS key for operator and CI roles to attach.
#
# IMPORTANT — LOCAL STATE
# -----------------------
# This config uses the LOCAL Terraform state backend (the default) on purpose.
# The resources here are exactly the resources a remote backend would store
# state in, so we have a chicken-and-egg problem: the bootstrap cannot store
# its own state inside the bucket it is creating.
#
# After `terraform apply`, commit the resulting `terraform.tfstate` file to
# secure storage (e.g. private internal Git repo or password-protected vault).
# Do NOT commit it to a public/shared repository — the file contains the KMS
# key ARN and other identifiers.
#
# Operationally this config is run ONCE per AWS account/region. See README.md
# for the runbook.
###############################################################################

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = merge(
      {
        Project     = var.project
        Component   = "terraform-bootstrap"
        ManagedBy   = "terraform"
        Environment = "shared"
      },
      var.tags
    )
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id   = data.aws_caller_identity.current.account_id
  region       = data.aws_region.current.name
  state_bucket = "biodata-registry-tf-state-${local.account_id}-${local.region}"
  lock_table   = "biodata-registry-tf-locks"
  kms_alias    = "alias/biodata-registry-tf-state"
  iam_policy   = "biodata-registry-tf-backend-access"
}

# ---------------------------------------------------------------------------
# KMS — customer-managed CMK with rotation, used for S3 SSE and DynamoDB SSE.
# ---------------------------------------------------------------------------

resource "aws_kms_key" "tf_state" {
  description             = "CMK for Allen BioData Registry Terraform remote state (S3 + DynamoDB)."
  enable_key_rotation     = true
  deletion_window_in_days = 30

  # Default key policy plus an explicit statement allowing root account
  # principals to administer the key. Resource-level grants for the IAM
  # policy below come through standard IAM evaluation.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnableRootPermissions"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${local.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      }
    ]
  })

  tags = {
    Name = local.kms_alias
  }
}

resource "aws_kms_alias" "tf_state" {
  name          = local.kms_alias
  target_key_id = aws_kms_key.tf_state.key_id
}

# ---------------------------------------------------------------------------
# S3 — Terraform state bucket.
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "tf_state" {
  bucket = local.state_bucket

  # Hard-fail an accidental destroy of the live state bucket.
  lifecycle {
    prevent_destroy = true
  }

  tags = {
    Name = local.state_bucket
  }
}

resource "aws_s3_bucket_versioning" "tf_state" {
  bucket = aws_s3_bucket.tf_state.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tf_state" {
  bucket = aws_s3_bucket.tf_state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.tf_state.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "tf_state" {
  bucket = aws_s3_bucket.tf_state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "tf_state" {
  bucket = aws_s3_bucket.tf_state.id

  # Prevent the lifecycle config from racing the versioning config on first
  # apply — versioning must be enabled before lifecycle rules referencing
  # noncurrent versions take effect.
  depends_on = [aws_s3_bucket_versioning.tf_state]

  rule {
    id     = "archive-noncurrent-versions"
    status = "Enabled"

    # Apply to all objects in the bucket.
    filter {}

    noncurrent_version_transition {
      noncurrent_days = 90
      storage_class   = "GLACIER"
    }

    # Defensive: clean up failed multipart uploads to avoid silent storage cost.
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# ---------------------------------------------------------------------------
# DynamoDB — Terraform state lock table.
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "tf_locks" {
  name         = local.lock_table
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }

  server_side_encryption {
    enabled = true
    # AWS-managed key for DynamoDB SSE keeps the bootstrap simple. The state
    # bucket itself uses the customer-managed CMK above.
  }

  point_in_time_recovery {
    enabled = true
  }

  lifecycle {
    prevent_destroy = true
  }

  tags = {
    Name = local.lock_table
  }
}

# ---------------------------------------------------------------------------
# IAM — scoped policy operators / CI roles attach to gain backend access.
#
# Grants ONLY the minimum actions Terraform needs to read/write state and
# acquire/release locks. No wildcards on bucket actions.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "backend_access" {
  statement {
    sid     = "S3ListStateBucket"
    effect  = "Allow"
    actions = ["s3:ListBucket"]
    resources = [
      aws_s3_bucket.tf_state.arn
    ]
  }

  statement {
    sid    = "S3ReadWriteStateObjects"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject"
    ]
    resources = [
      "${aws_s3_bucket.tf_state.arn}/*"
    ]
  }

  statement {
    sid    = "DynamoDBStateLock"
    effect = "Allow"
    actions = [
      "dynamodb:PutItem",
      "dynamodb:GetItem",
      "dynamodb:DeleteItem"
    ]
    resources = [
      aws_dynamodb_table.tf_locks.arn
    ]
  }

  statement {
    sid    = "KMSStateEncryption"
    effect = "Allow"
    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:GenerateDataKey",
      "kms:DescribeKey"
    ]
    resources = [
      aws_kms_key.tf_state.arn
    ]
  }
}

resource "aws_iam_policy" "backend_access" {
  name        = local.iam_policy
  description = "Scoped access to the Allen BioData Registry Terraform remote state backend (S3 + DynamoDB + KMS)."
  policy      = data.aws_iam_policy_document.backend_access.json
}

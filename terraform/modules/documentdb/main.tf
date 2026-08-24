###############################################################################
# Allen BioData Registry PoC — documentdb module
#
# Provisions:
#   * KMS CMK (only if var.kms_key_arn is null) — storage + Secrets at rest
#   * DB subnet group spanning the private subnets exported by the vpc module
#   * DocumentDB cluster parameter group (family = docdb5.0) with TLS and
#     audit logging enabled
#   * DocumentDB cluster (engine docdb 5.0.0 — MongoDB 5.0 wire protocol)
#   * One or more cluster instances (default 1× db.r6g.large for PoC)
#   * Random master + read-only passwords
#   * Master credentials secret (Secrets Manager, KMS-encrypted)
#   * Read-only credentials secret (Secrets Manager, KMS-encrypted)
#   * null_resource + local-exec that runs mongosh to create the
#     biodata_reader DB user post-apply
#
# Validates: R28.4 (DocumentDB read layer for aind-data-access-api),
# R31.3 (KMS encryption at rest for DocumentDB), R32.2 (terraform apply
# provisions DocumentDB).
#
# Design references:
#   * design.md §Data Models.DocumentDB Document Shape
#   * design.md §Design Decisions.DocumentDB Access Model and Trust Boundary
#
# Trust-boundary caveat (see README "IAM database authentication" section):
# DocumentDB does NOT support RDS-style IAM database authentication (short-
# lived auth tokens). The trust boundary the design relies on is a
# composition of:
#   1. VPC isolation — cluster has no public endpoint and is reachable only
#      from inside the Allen Institute VPC
#   2. IAM-protected master / read-only credentials in Secrets Manager
#   3. TLS in transit (the default in DocumentDB 5.0)
#   4. A read-only DB user (biodata_reader) bound to the `readAnyDatabase`
#      role; aind-data-access-api consumers connect as this user
#   5. Client-library RLS-equivalent filtering on the denormalized
#      space_id / org_id / is_sensitive fields written by the
#      Indexing_Lambda
#
# IAM authenticates the *connection*, not the *individual request*; end-user
# identity is carried by the client library into every query's filter
# clause.
###############################################################################

locals {
  common_tags = merge(
    {
      Project     = var.project
      Environment = var.environment
      Module      = "documentdb"
      ManagedBy   = "terraform"
    },
    var.tags,
  )

  cluster_identifier   = "${var.name_prefix}-documentdb"
  parameter_group_name = "${var.name_prefix}-documentdb-params"
  subnet_group_name    = "${var.name_prefix}-documentdb-subnets"
  master_secret_name   = "${var.name_prefix}-documentdb-master"
  readonly_secret_name = "${var.name_prefix}-documentdb-readonly"

  # Use the caller-supplied KMS key when provided; otherwise the dedicated
  # CMK provisioned by this module.
  effective_kms_key_arn = (
    var.kms_key_arn != null
    ? var.kms_key_arn
    : aws_kms_key.this[0].arn
  )

  port = 27017
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

###############################################################################
# KMS CMK (provisioned only if the caller did not pass an existing key ARN)
#
# Used for DocumentDB storage encryption (R31.3) and Secrets Manager secret
# encryption. Key rotation is enabled. The default policy grants the
# account root full access; downstream IAM policies on the consumer Lambdas
# get scoped Decrypt access via the secret's resource policy + KMS key
# grants.
###############################################################################

resource "aws_kms_key" "this" {
  count = var.kms_key_arn == null ? 1 : 0

  description             = "CMK for ${local.cluster_identifier} storage and Secrets Manager."
  enable_key_rotation     = true
  deletion_window_in_days = 30

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnableRootAccountAdmin"
        Effect    = "Allow"
        Principal = { AWS = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "AllowRDSUse"
        Effect    = "Allow"
        Principal = { Service = "rds.amazonaws.com" }
        Action = [
          "kms:Decrypt",
          "kms:DescribeKey",
          "kms:Encrypt",
          "kms:GenerateDataKey*",
          "kms:ReEncrypt*",
          "kms:CreateGrant",
        ]
        Resource = "*"
      },
      {
        Sid       = "AllowSecretsManagerUse"
        Effect    = "Allow"
        Principal = { Service = "secretsmanager.amazonaws.com" }
        Action = [
          "kms:Decrypt",
          "kms:DescribeKey",
          "kms:Encrypt",
          "kms:GenerateDataKey*",
          "kms:ReEncrypt*",
          "kms:CreateGrant",
        ]
        Resource = "*"
      },
    ]
  })

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-documentdb-cmk"
  })
}

resource "aws_kms_alias" "this" {
  count = var.kms_key_arn == null ? 1 : 0

  name          = "alias/${var.name_prefix}-documentdb"
  target_key_id = aws_kms_key.this[0].key_id
}

###############################################################################
# Random passwords
#
# DocumentDB master password disallowed characters: / " @ and whitespace.
# The plaintext is held in Terraform state; downstream consumers MUST fetch
# both passwords from Secrets Manager (the *_password is intentionally NOT
# exported as a Terraform output).
###############################################################################

resource "random_password" "master" {
  length      = 32
  special     = true
  upper       = true
  lower       = true
  numeric     = true
  min_lower   = 2
  min_upper   = 2
  min_numeric = 2
  min_special = 2
  # DocumentDB master password disallowed characters: / " @ whitespace
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

resource "random_password" "readonly" {
  length           = 32
  special          = true
  upper            = true
  lower            = true
  numeric          = true
  min_lower        = 2
  min_upper        = 2
  min_numeric      = 2
  min_special      = 2
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

###############################################################################
# DB subnet group
###############################################################################

resource "aws_docdb_subnet_group" "this" {
  name        = local.subnet_group_name
  subnet_ids  = var.private_subnet_ids
  description = "Private subnets for ${local.cluster_identifier} (Allen BioData Registry PoC)."

  tags = merge(local.common_tags, {
    Name = local.subnet_group_name
  })
}

###############################################################################
# Cluster parameter group
#
# audit_logs = enabled  → R28 production hardening path (post-hoc detection
#                         of queries that omit the RLS-equivalent client
#                         filter; design.md §Design Decisions.DocumentDB
#                         Access Model)
# tls        = enabled  → in-transit encryption; required by the trust model.
#                         DocumentDB 5.0 has tls=enabled as the default but
#                         we set it explicitly so the value is reviewable in
#                         the parameter group.
###############################################################################

resource "aws_docdb_cluster_parameter_group" "this" {
  name        = local.parameter_group_name
  family      = "docdb5.0"
  description = "Parameter group for ${local.cluster_identifier}: TLS + audit logging."

  parameter {
    name  = "audit_logs"
    value = "enabled"
  }

  parameter {
    name  = "tls"
    value = "enabled"
  }

  tags = merge(local.common_tags, {
    Name = local.parameter_group_name
  })
}

###############################################################################
# DocumentDB cluster
###############################################################################

resource "aws_docdb_cluster" "this" {
  cluster_identifier              = local.cluster_identifier
  engine                          = "docdb"
  engine_version                  = var.engine_version
  master_username                 = var.master_username
  master_password                 = random_password.master.result
  port                            = local.port
  db_subnet_group_name            = aws_docdb_subnet_group.this.name
  db_cluster_parameter_group_name = aws_docdb_cluster_parameter_group.this.name
  vpc_security_group_ids          = var.security_group_ids
  storage_encrypted               = true
  kms_key_id                      = local.effective_kms_key_arn
  backup_retention_period         = var.backup_retention_period
  preferred_backup_window         = var.preferred_backup_window
  preferred_maintenance_window    = var.preferred_maintenance_window
  skip_final_snapshot             = var.skip_final_snapshot
  apply_immediately               = var.apply_immediately
  enabled_cloudwatch_logs_exports = var.enabled_cloudwatch_log_exports
  deletion_protection             = var.deletion_protection

  final_snapshot_identifier = var.skip_final_snapshot ? null : "${local.cluster_identifier}-final"

  tags = merge(local.common_tags, {
    Name = local.cluster_identifier
  })

  lifecycle {
    # The master password lives in Secrets Manager — Terraform should not
    # try to update the cluster every time the secret is rotated out-of-band.
    ignore_changes = [
      master_password,
      final_snapshot_identifier,
    ]
  }
}

###############################################################################
# Cluster instance(s)
#
# PoC default: one db.r6g.large. DocumentDB 5.0 dropped support for
# t3-class instances; r6g.large is the smallest current-generation class.
# Production should run ≥2 instances spread across AZs (see README HA
# trade-off).
###############################################################################

resource "aws_docdb_cluster_instance" "this" {
  count = var.instance_count

  identifier                   = "${local.cluster_identifier}-${count.index}"
  cluster_identifier           = aws_docdb_cluster.this.id
  instance_class               = var.instance_class
  engine                       = "docdb"
  preferred_maintenance_window = var.preferred_maintenance_window
  apply_immediately            = var.apply_immediately
  auto_minor_version_upgrade   = true

  tags = merge(local.common_tags, {
    Name = "${local.cluster_identifier}-${count.index}"
    Role = count.index == 0 ? "writer" : "reader"
  })
}

###############################################################################
# Secrets Manager — master credentials
#
# Stored as a structured JSON blob. Consumers fetch this via
# `aws secretsmanager get-secret-value`.
###############################################################################

resource "aws_secretsmanager_secret" "master" {
  name                    = local.master_secret_name
  description             = "Master DB credentials for ${local.cluster_identifier}."
  kms_key_id              = local.effective_kms_key_arn
  recovery_window_in_days = var.secret_recovery_window_in_days

  tags = merge(local.common_tags, {
    Name = local.master_secret_name
    Role = "documentdb-master"
  })
}

resource "aws_secretsmanager_secret_version" "master" {
  secret_id = aws_secretsmanager_secret.master.id

  secret_string = jsonencode({
    username       = var.master_username
    password       = random_password.master.result
    host           = aws_docdb_cluster.this.endpoint
    port           = local.port
    engine         = "docdb"
    reader_host    = aws_docdb_cluster.this.reader_endpoint
    dbName         = var.db_name
    ssl            = true
    sslCABundleUrl = var.tls_ca_bundle_url
  })
}

###############################################################################
# Secrets Manager — read-only credentials
#
# The read-only password is generated up-front by random_password.readonly
# so that the Secrets Manager value is real (not a placeholder) on the very
# first apply. The mongosh bootstrap below uses this same password to
# create the corresponding DB user.
#
# This is the secret the aind-data-access-api consumers receive IAM access
# to — they do NOT have access to the master secret. Separate secrets allow
# rotating consumer credentials without touching the master.
###############################################################################

resource "aws_secretsmanager_secret" "readonly" {
  name                    = local.readonly_secret_name
  description             = "Read-only DB credentials for ${local.cluster_identifier}, used by aind-data-access-api consumers."
  kms_key_id              = local.effective_kms_key_arn
  recovery_window_in_days = var.secret_recovery_window_in_days

  tags = merge(local.common_tags, {
    Name = local.readonly_secret_name
    Role = "documentdb-readonly"
  })
}

resource "aws_secretsmanager_secret_version" "readonly" {
  secret_id = aws_secretsmanager_secret.readonly.id

  secret_string = jsonencode({
    username       = var.readonly_username
    password       = random_password.readonly.result
    host           = aws_docdb_cluster.this.endpoint
    port           = local.port
    engine         = "docdb"
    reader_host    = aws_docdb_cluster.this.reader_endpoint
    dbName         = var.db_name
    ssl            = true
    sslCABundleUrl = var.tls_ca_bundle_url
    role           = "readAnyDatabase"
  })
}

###############################################################################
# Read-only DB user bootstrap (null_resource + local-exec)
#
# DocumentDB has no Terraform-native "user" resource — users are created
# via the MongoDB wire protocol. We run mongosh from the Terraform
# operator's workstation after the cluster instance is up, and idempotently
# create the `biodata_reader` user with the `readAnyDatabase` role.
#
# Operator pre-requisites (documented in README):
#   * mongosh installed on PATH (or override via var.mongosh_binary)
#   * The `global-bundle.pem` CA file downloaded to a known location;
#     the bootstrap also fetches it on demand if missing.
#   * VPC reach to the cluster — the operator must be on the Allen
#     Institute VPN, a bastion, or running terraform from inside the VPC
#     (e.g. via SSM session manager).
#
# The bootstrap is wrapped in a single-line bash invocation because
# Terraform's local-exec disables interactive shells; we use --eval to
# pass the user-creation JS to mongosh. Idempotency is achieved by
# wrapping createUser in a try/catch that updateUser-s on the duplicate
# code (51003) so subsequent applies are no-ops.
#
# Production hardening: replace this with a one-shot Lambda inside the VPC
# triggered by the same Terraform apply (no operator-workstation reach
# required). Out of scope for the PoC — Task 8 will revisit.
###############################################################################

resource "null_resource" "readonly_user_bootstrap" {
  count = var.enable_readonly_user_bootstrap ? 1 : 0

  # Re-run if any of the inputs that affect the user / connection change.
  triggers = {
    cluster_endpoint  = aws_docdb_cluster.this.endpoint
    readonly_username = var.readonly_username
    readonly_password = random_password.readonly.result
    db_name           = var.db_name
    instance_ready    = join(",", aws_docdb_cluster_instance.this[*].id)
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    environment = {
      DOCDB_HOST       = aws_docdb_cluster.this.endpoint
      DOCDB_PORT       = tostring(local.port)
      DOCDB_MASTER_USR = var.master_username
      DOCDB_MASTER_PWD = random_password.master.result
      DOCDB_RO_USR     = var.readonly_username
      DOCDB_RO_PWD     = random_password.readonly.result
      DOCDB_DB_NAME    = var.db_name
      DOCDB_CA_URL     = var.tls_ca_bundle_url
      MONGOSH_BIN      = var.mongosh_binary
    }

    # The script:
    #   1. Downloads the global CA bundle to a temp file (idempotent).
    #   2. Connects to the cluster as master via TLS.
    #   3. Creates the read-only user, or updates the password + roles if
    #      the user already exists (DocumentDB error code 51003).
    command = <<-EOT
      set -euo pipefail

      CA_FILE="$(mktemp -t docdb-ca-XXXXXX.pem)"
      trap 'rm -f "$CA_FILE"' EXIT
      curl -sSfL "$DOCDB_CA_URL" -o "$CA_FILE"

      "$MONGOSH_BIN" \
        --quiet \
        --tls \
        --tlsCAFile "$CA_FILE" \
        --host "$DOCDB_HOST" \
        --port "$DOCDB_PORT" \
        --username "$DOCDB_MASTER_USR" \
        --password "$DOCDB_MASTER_PWD" \
        --eval "
          const targetDb = '$DOCDB_DB_NAME';
          const adminDb = db.getSiblingDB('admin');
          const userDoc = {
            user: '$DOCDB_RO_USR',
            pwd:  '$DOCDB_RO_PWD',
            roles: [{ role: 'readAnyDatabase', db: 'admin' }]
          };
          try {
            adminDb.runCommand({ createUser: userDoc.user, pwd: userDoc.pwd, roles: userDoc.roles });
            print('biodata_reader created');
          } catch (e) {
            if (e.code === 51003 || /already exists/i.test(String(e))) {
              adminDb.runCommand({ updateUser: userDoc.user, pwd: userDoc.pwd, roles: userDoc.roles });
              print('biodata_reader updated (already existed)');
            } else {
              throw e;
            }
          }
        "
    EOT
  }

  depends_on = [
    aws_docdb_cluster_instance.this,
    aws_secretsmanager_secret_version.readonly,
  ]
}

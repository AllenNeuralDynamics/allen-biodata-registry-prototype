###############################################################################
# Allen BioData Registry PoC — aurora module
#
# Provisions:
#   * Aurora PostgreSQL Serverless v2 cluster (engine 16.13, source-of-truth
#     for the registry — R1.7, R28.2)
#   * Custom DB cluster parameter group enabling logical replication
#     (rds.logical_replication = 1) and the pgvector + pgaudit +
#     pg_stat_statements shared preload libraries (R28.1, R28.2)
#   * KMS CMK for storage + secret encryption (created if not provided —
#     R31.3)
#   * DB subnet group spanning the VPC's private subnets (R31.1, R31.3)
#   * One or more Aurora Serverless v2 cluster instances (writer + readers)
#     with Performance Insights and Enhanced Monitoring enabled
#   * Secrets Manager secret holding the master credentials, KMS-encrypted,
#     consumed by every business Lambda (R31.1)
#   * Post-apply `null_resource` that creates the `vector` and `pg_trgm`
#     extensions and the `biodata_cdc` logical replication slot via psql
#     (R28.1, R28.2)
#   * IAM database authentication enabled (free, doesn't conflict with
#     master password auth)
#
# Validates: R1.7, R28.1, R28.2, R31.1, R31.3, R32.2.
# Design reference: design.md §Architecture.CDC Pipeline Architecture,
# §IaC.Terraform Modules (`aurora`).
#
# Out of scope (handled in later tasks):
#   * Schema migrations (CREATE TABLE ...) — Tasks 7.1-7.7 + migration
#     runner (Task 8.1).
#   * CDC transport (EventBridge Pipes / MSK) — Task 17.1 (`cdc-pipeline`
#     module).
#   * Master password rotation — commented block included; production
#     should wire to the AWS-managed RDS rotation Lambda.
###############################################################################

locals {
  common_tags = merge(
    {
      Project     = var.project
      Environment = var.environment
      Module      = "aurora"
      ManagedBy   = "terraform"
    },
    var.tags,
  )

  cluster_identifier         = "${var.name_prefix}-aurora"
  parameter_group_name       = "${var.name_prefix}-aurora-pg16"
  db_subnet_group_name       = "${var.name_prefix}-aurora-subnets"
  master_secret_name         = "${var.name_prefix}-aurora-master"
  kms_key_alias              = "alias/${var.name_prefix}-aurora"
  parameter_group_family     = "aurora-postgresql16"
  cluster_instance_id_prefix = "${var.name_prefix}-aurora-instance"

  # Use the externally provided CMK if given; otherwise the module creates
  # one and we read the ARN of the created key. Computed ARN keeps the
  # downstream wiring (storage_encrypted, secret kms_key_id) uniform.
  effective_kms_key_arn = var.kms_key_arn != null ? var.kms_key_arn : aws_kms_key.aurora[0].arn

  # Tracked here so the cluster_identifier and other names are easy to
  # reference from the README + outputs without repeating the format.
  port = 5432
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_partition" "current" {}

###############################################################################
# KMS — created only when no external CMK is supplied.

resource "aws_kms_key" "aurora" {
  count = var.kms_key_arn == null ? 1 : 0

  description             = "CMK for Allen BioData Registry Aurora storage + Secrets Manager secret (${var.name_prefix})"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  # Default key policy: account-root admin, plus allow RDS and Secrets
  # Manager service principals to use the key on behalf of the account.
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
        Sid    = "AllowRDSUse"
        Effect = "Allow"
        Principal = {
          Service = "rds.amazonaws.com"
        }
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
        Sid    = "AllowSecretsManagerUse"
        Effect = "Allow"
        Principal = {
          Service = "secretsmanager.amazonaws.com"
        }
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
    Name = "${var.name_prefix}-aurora-cmk"
  })
}

resource "aws_kms_alias" "aurora" {
  count = var.kms_key_arn == null ? 1 : 0

  name          = local.kms_key_alias
  target_key_id = aws_kms_key.aurora[0].key_id
}

###############################################################################
# Subnet group + parameter group

resource "aws_db_subnet_group" "this" {
  name       = local.db_subnet_group_name
  subnet_ids = var.private_subnet_ids

  description = "Private subnets for ${local.cluster_identifier} (Allen BioData Registry PoC)"

  tags = merge(local.common_tags, {
    Name = local.db_subnet_group_name
  })
}

# Cluster parameter group enabling logical replication, pgvector, and
# pg_stat_statements. Logical replication and shared_preload_libraries both
# require pending-reboot — applying these means the first apply provisions
# the cluster with the values already in place; later changes will reboot
# the cluster on the next maintenance window. (R28.1, R28.2)
resource "aws_rds_cluster_parameter_group" "this" {
  name        = local.parameter_group_name
  family      = local.parameter_group_family
  description = "Aurora PostgreSQL 16 cluster parameter group for ${local.cluster_identifier} - enables logical replication for CDC and preloads pgvector."

  # Enable logical replication so downstream CDC (Task 17.1) can attach a
  # logical replication slot. (R28.1, R28.2)
  parameter {
    name         = "rds.logical_replication"
    value        = "1"
    apply_method = "pending-reboot"
  }

  # Preload pgaudit (security audit trail) and pg_stat_statements (operability).
  # pgvector is NOT a preloadable library on Aurora PostgreSQL — it's enabled
  # via `CREATE EXTENSION vector;` after the cluster is up (the migration
  # runner does this in 0001 / equivalent). Listing it here causes RDS to
  # reject the parameter group with "Invalid parameter value: vector".
  parameter {
    name         = "shared_preload_libraries"
    value        = "pg_stat_statements,pgaudit"
    apply_method = "pending-reboot"
  }

  # DDL audit trail in CloudWatch Logs.
  parameter {
    name         = "log_statement"
    value        = "ddl"
    apply_method = "immediate"
  }

  # CDC tuning — give logical replication enough headroom and disable the
  # WAL-sender idle timeout so a stalled CDC consumer is not silently
  # disconnected mid-stream. (R28.1, R28.2)
  parameter {
    name         = "max_replication_slots"
    value        = "10"
    apply_method = "pending-reboot"
  }

  parameter {
    name         = "max_wal_senders"
    value        = "10"
    apply_method = "pending-reboot"
  }

  # 0 disables the timeout. The CDC pipeline (Task 17.1) buffers events
  # through EventBridge Pipes / SQS, so an in-flight backlog should not
  # cause Aurora to terminate the slot.
  parameter {
    name         = "wal_sender_timeout"
    value        = "0"
    apply_method = "immediate"
  }

  # Capture longer query text in pg_stat_activity / pg_stat_statements —
  # very useful for debugging large-payload Registration/Validation
  # queries during the PoC.
  parameter {
    name         = "track_activity_query_size"
    value        = "4096"
    apply_method = "pending-reboot"
  }

  tags = merge(local.common_tags, {
    Name = local.parameter_group_name
  })
}

# DB instance parameter group — applied to every cluster instance. Logs any
# statement whose total runtime exceeds 1 second so the team has slow-query
# visibility from day one without paying for full statement logging.
resource "aws_db_parameter_group" "this" {
  name        = "${var.name_prefix}-aurora-pg16-instance"
  family      = local.parameter_group_family
  description = "Aurora PostgreSQL 16 instance parameter group for ${local.cluster_identifier} - slow-query logging."

  parameter {
    name         = "log_min_duration_statement"
    value        = "1000"
    apply_method = "immediate"
  }

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-aurora-pg16-instance"
  })
}

###############################################################################
# Master password generator

# Random password used as the master credential. Aurora rejects characters
# `/@" '`, so they are excluded explicitly. The password is written into
# Secrets Manager and never echoed to outputs.
resource "random_password" "master" {
  length           = 32
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"

  keepers = {
    cluster_identifier = local.cluster_identifier
  }
}

###############################################################################
# Enhanced Monitoring IAM role
#
# Required when monitoring_interval_seconds > 0. Aurora pushes per-instance
# OS metrics into CloudWatch Logs as RDSOSMetrics. The managed policy
# `AmazonRDSEnhancedMonitoringRole` is the documented requirement.

resource "aws_iam_role" "rds_monitoring" {
  count = var.monitoring_interval_seconds > 0 ? 1 : 0

  name = "${var.name_prefix}-aurora-monitoring"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "monitoring.rds.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-aurora-monitoring"
  })
}

resource "aws_iam_role_policy_attachment" "rds_monitoring" {
  count = var.monitoring_interval_seconds > 0 ? 1 : 0

  role       = aws_iam_role.rds_monitoring[0].name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}

###############################################################################
# Aurora cluster + Serverless v2 instance(s)

resource "aws_rds_cluster" "this" {
  cluster_identifier = local.cluster_identifier

  engine         = "aurora-postgresql"
  engine_mode    = "provisioned" # Required for Serverless v2; "serverless" is v1.
  engine_version = var.engine_version

  database_name   = var.db_name
  master_username = var.master_username
  master_password = random_password.master.result

  port = local.port

  db_subnet_group_name            = aws_db_subnet_group.this.name
  vpc_security_group_ids          = var.security_group_ids
  db_cluster_parameter_group_name = aws_rds_cluster_parameter_group.this.name

  storage_encrypted = true
  kms_key_id        = local.effective_kms_key_arn

  iam_database_authentication_enabled = var.iam_database_authentication_enabled

  backup_retention_period      = var.backup_retention_days
  preferred_backup_window      = var.preferred_backup_window
  preferred_maintenance_window = var.preferred_maintenance_window

  # PoC trade-offs documented in README — production MUST flip these.
  skip_final_snapshot       = var.skip_final_snapshot
  deletion_protection       = var.deletion_protection
  final_snapshot_identifier = var.skip_final_snapshot ? null : "${local.cluster_identifier}-final"

  enabled_cloudwatch_logs_exports = ["postgresql"]

  # PoC convenience — apply parameter / config changes immediately rather
  # than waiting for the maintenance window. Production should set this
  # back to false to coordinate disruptive changes.
  apply_immediately = true

  serverlessv2_scaling_configuration {
    min_capacity = var.min_capacity_acu
    max_capacity = var.max_capacity_acu
  }

  tags = merge(local.common_tags, {
    Name = local.cluster_identifier
  })

  # The master_password is stored in Secrets Manager — Terraform should not
  # try to update the cluster every time the secret is rotated out-of-band.
  lifecycle {
    ignore_changes = [
      master_password,
    ]
  }
}

resource "aws_rds_cluster_instance" "this" {
  count = var.instance_count

  identifier         = "${local.cluster_instance_id_prefix}-${count.index + 1}"
  cluster_identifier = aws_rds_cluster.this.id

  engine         = aws_rds_cluster.this.engine
  engine_version = aws_rds_cluster.this.engine_version

  instance_class = "db.serverless"

  db_subnet_group_name    = aws_db_subnet_group.this.name
  db_parameter_group_name = aws_db_parameter_group.this.name

  # Performance Insights uses the same KMS key as the cluster.
  performance_insights_enabled    = var.performance_insights_enabled
  performance_insights_kms_key_id = var.performance_insights_enabled ? local.effective_kms_key_arn : null

  # Enhanced Monitoring — 60s interval per Aurora best practice. Set
  # var.monitoring_interval_seconds = 0 to disable.
  monitoring_interval = var.monitoring_interval_seconds
  monitoring_role_arn = var.monitoring_interval_seconds > 0 ? aws_iam_role.rds_monitoring[0].arn : null

  publicly_accessible = false

  apply_immediately = true

  tags = merge(local.common_tags, {
    Name = "${local.cluster_instance_id_prefix}-${count.index + 1}"
    Role = count.index == 0 ? "writer" : "reader"
  })
}

###############################################################################
# Secrets Manager — master credentials

resource "aws_secretsmanager_secret" "master" {
  name        = local.master_secret_name
  description = "Aurora master credentials for ${local.cluster_identifier} (Allen BioData Registry PoC)"
  kms_key_id  = local.effective_kms_key_arn

  recovery_window_in_days = var.secrets_recovery_window_days

  tags = merge(local.common_tags, {
    Name = local.master_secret_name
  })
}

resource "aws_secretsmanager_secret_version" "master" {
  secret_id = aws_secretsmanager_secret.master.id

  secret_string = jsonencode({
    username = var.master_username
    password = random_password.master.result
    host     = aws_rds_cluster.this.endpoint
    port     = local.port
    dbname   = var.db_name
    engine   = "postgres"

    # The CDC pipeline (Task 17.1) and the migration runner (Task 8.1) use
    # this slot name; carrying it on the secret keeps the consumer wiring
    # in one place.
    cdc_replication_slot = var.cdc_replication_slot_name
  })
}

###############################################################################
# Secrets Manager — rotation (DISABLED for PoC)
#
# TODO (Task 10 / production): enable rotation. Aurora supports the AWS-
# managed rotation Lambda template `SecretsManagerRDSPostgreSQLRotation`,
# which generates a new password, ALTERs the master role, and updates the
# secret atomically. Uncomment and supply a Lambda ARN to enable.
#
# resource "aws_secretsmanager_secret_rotation" "master" {
#   secret_id           = aws_secretsmanager_secret.master.id
#   rotation_lambda_arn = var.rotation_lambda_arn
#   rotation_rules {
#     automatically_after_days = 30
#   }
# }
#
###############################################################################

###############################################################################
# Logical replication slot + extension bootstrap (`null_resource`)
#
# Runs ONCE after the cluster + writer instance are up. Connects via psql
# using the master credentials and:
#   1. Creates the `vector` extension (pgvector) — note the extension name
#      is `vector`, not `pgvector`. The library is preloaded via
#      shared_preload_libraries above; CREATE EXTENSION activates it for
#      the database.
#   2. Creates the `pg_trgm` extension for trigram similarity (used by
#      Duplicates_Lambda's SQL similarity() check).
#   3. Creates the `biodata_cdc` logical replication slot using the
#      `pgoutput` plugin so the CDC pipeline (Task 17.1) can attach.
#
# Triggers ensure the resource only re-runs when the cluster endpoint or
# slot name change — not on every apply.
#
# REQUIREMENTS:
#   - `psql` must be on the operator's PATH (libpq client). On the team's
#     workstations: `brew install libpq && brew link --force libpq` (macOS)
#     or `apt-get install postgresql-client` (Linux).
#   - The operator must have network reach to the cluster's writer
#     endpoint over port 5432. Aurora is in private subnets, so the
#     operator MUST be inside the VPC — typically via SSM session, Cloud9
#     workspace, AWS Client VPN, or VPC Reachability Analyzer-tested
#     bastion.
#
# TODO (Task 10): if `terraform apply` is run from outside the VPC, set
#   bootstrap_slot_via_null_resource = false and have one of the following
#   handle bootstrap instead:
#     (a) The schema migration runner (Task 8.1) — recommended path.
#         Idempotent, runs the same SQL, lives alongside the rest of the
#         schema migrations.
#     (b) A small Python Lambda invoked via `aws_lambda_invocation`. More
#         code than (a) but zero operator friction and keeps the bring-up
#         contained in the Terraform graph. The Lambda would resolve the
#         master secret, connect via psycopg with sslmode=require, and run
#         the same three SQL statements below.
###############################################################################

resource "null_resource" "bootstrap_slot_and_extensions" {
  count = var.bootstrap_slot_via_null_resource ? 1 : 0

  triggers = {
    cluster_endpoint      = aws_rds_cluster.this.endpoint
    replication_slot_name = var.cdc_replication_slot_name
    secret_version_id     = aws_secretsmanager_secret_version.master.version_id
    cluster_resource_id   = aws_rds_cluster.this.cluster_resource_id
  }

  # The SQL is intentionally `IF NOT EXISTS` and a guarded
  # `pg_create_logical_replication_slot` so re-running this provisioner
  # is a no-op once the slot and extensions exist.
  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    environment = {
      AURORA_SECRET_ARN = aws_secretsmanager_secret.master.arn
      AURORA_HOST       = aws_rds_cluster.this.endpoint
      AURORA_PORT       = tostring(local.port)
      AURORA_DBNAME     = var.db_name
      AURORA_USERNAME   = var.master_username
      AWS_REGION        = data.aws_region.current.name
      SLOT_NAME         = var.cdc_replication_slot_name
    }

    command = <<-EOT
      set -euo pipefail

      echo "[aurora-bootstrap] Resolving master credentials from Secrets Manager..."
      PGPASSWORD="$(aws secretsmanager get-secret-value \
        --secret-id "$AURORA_SECRET_ARN" \
        --region "$AWS_REGION" \
        --query SecretString --output text \
        | python3 -c 'import sys, json; print(json.load(sys.stdin)["password"])')"
      export PGPASSWORD

      PSQL_OPTS=(
        --host="$AURORA_HOST"
        --port="$AURORA_PORT"
        --username="$AURORA_USERNAME"
        --dbname="$AURORA_DBNAME"
        --set=ON_ERROR_STOP=1
        --set=sslmode=require
        --no-password
      )

      echo "[aurora-bootstrap] Creating extensions (vector, pg_trgm)..."
      psql "$${PSQL_OPTS[@]}" <<SQL
        CREATE EXTENSION IF NOT EXISTS vector;
        CREATE EXTENSION IF NOT EXISTS pg_trgm;
SQL

      echo "[aurora-bootstrap] Ensuring logical replication slot '$SLOT_NAME' exists..."
      psql "$${PSQL_OPTS[@]}" <<SQL
        SELECT pg_create_logical_replication_slot('$SLOT_NAME', 'pgoutput')
        WHERE NOT EXISTS (
          SELECT 1 FROM pg_replication_slots WHERE slot_name = '$SLOT_NAME'
        );
SQL

      echo "[aurora-bootstrap] Done."
    EOT
  }

  depends_on = [
    aws_rds_cluster.this,
    aws_rds_cluster_instance.this,
    aws_secretsmanager_secret_version.master,
  ]
}

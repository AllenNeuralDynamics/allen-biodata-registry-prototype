###############################################################################
# Allen BioData Registry PoC — elasticache module
#
# Provisions:
#   * Dedicated KMS CMK (key rotation enabled) used for the replication
#     group's at-rest encryption AND the AUTH-token Secrets Manager secret.
#   * ElastiCache subnet group spanning the VPC's private subnets.
#   * ElastiCache parameter group on family `redis7` with:
#       - maxmemory-policy = allkeys-lru   (LRU eviction at memory pressure)
#       - notify-keyspace-events = ""      (disabled; expensive at scale)
#       - timeout = 300                    (close idle clients after 5 min)
#   * ElastiCache replication group (Redis 7.1) — 2 nodes by default
#     (1 primary + 1 replica), automatic failover + Multi-AZ on, KMS at
#     rest, TLS in-transit, AUTH token required.
#   * Secrets Manager secret holding `{auth_token, host, port}` so consuming
#     Lambdas resolve everything in one GetSecretValue call.
#
# Validates:
#   * R20.1 — 4-tier cache (tiers are key-prefix conventions enforced by the
#     application layer; the module provisions one shared cluster).
#   * R31.3 — KMS encryption at rest for ElastiCache Redis.
#   * R32.2 — terraform apply provisions ElastiCache as part of the stack.
#
# Tier separation (Redis has no SQL-style namespaces):
#   * API_Cache             keys `api:{method}:{path}:{query_hash}:{user_scope_hash}`   5-min TTL
#   * NL_Cache              keys `nl:{sha256(normalized_query)}`                        30-min TTL
#   * Schema_Cache          keys `schema:{org_id|_biodata}:{name}:{version}`            24-h TTL
#   * Access_Filter_Cache   keys `access:{user_id}`                                     5-min TTL
# The shared Lambda Layer (Task 12.1) is the only writer; it owns prefix
# discipline. See README.md for the full key schema. No tier-level Terraform
# resources exist by design — Redis 7's per-key TTL handles the per-tier
# TTLs; Terraform only provisions the cluster.
#
# HA model: PoC default is 1 primary + 1 replica with Multi-AZ failover
# (~30s typical promotion time). Lambdas tolerate Redis unavailability by
# falling through to Aurora (R20.7), so the cache is never on the
# correctness path. Document in the Lambda Layer that Redis clients should
# retry once on connection failure to absorb the failover gap.
###############################################################################

locals {
  common_tags = merge(
    {
      Project     = var.project
      Environment = var.environment
      Module      = "elasticache"
      ManagedBy   = "terraform"
    },
    var.tags,
  )

  replication_group_id = "${var.name_prefix}-redis"
  subnet_group_name    = "${var.name_prefix}-redis-subnet"
  parameter_group_name = "${var.name_prefix}-redis-params"
  auth_secret_name     = "${var.name_prefix}-redis-auth-token"

  # Redis 7.x always uses the redis7 parameter-group family.
  parameter_family = "redis7"
}

###############################################################################
# KMS CMK
#
# A dedicated CMK with key rotation enabled. Used to encrypt:
#   * the Redis replication group at rest (R31.3)
#   * the Secrets Manager secret holding the AUTH token
#
# The default key policy grants the account root full access; downstream
# Lambda execution-role policies are scoped to kms:Decrypt on this key ARN.
###############################################################################

resource "aws_kms_key" "this" {
  description             = "CMK for ${local.replication_group_id} at-rest encryption and AUTH-token secret."
  enable_key_rotation     = true
  deletion_window_in_days = 30

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-redis-kms"
  })
}

resource "aws_kms_alias" "this" {
  name          = "alias/${var.name_prefix}-redis"
  target_key_id = aws_kms_key.this.key_id
}

###############################################################################
# Subnet group
###############################################################################

resource "aws_elasticache_subnet_group" "this" {
  name        = local.subnet_group_name
  description = "Private subnets for ${local.replication_group_id}"
  subnet_ids  = var.private_subnet_ids

  tags = merge(local.common_tags, {
    Name = local.subnet_group_name
  })
}

###############################################################################
# Parameter group
#
# Family redis7. We override exactly what the 4-tier cache needs:
#   * maxmemory-policy = allkeys-lru    — evict LRU when memory fills.
#                                         TTL governs correctness; LRU
#                                         governs capacity.
#   * notify-keyspace-events = ""       — explicit default. Lambdas don't
#                                         need keyspace notifications for
#                                         the PoC; documenting the choice
#                                         prevents accidental enablement,
#                                         which is expensive at scale.
#   * timeout = 300                     — close clients idle for more than
#                                         5 min (Lambda warm window is
#                                         shorter, so this only catches
#                                         leaked connections).
###############################################################################

resource "aws_elasticache_parameter_group" "this" {
  name        = local.parameter_group_name
  family      = local.parameter_family
  description = "Allen BioData Registry 4-tier cache parameters (LRU eviction, 5-min idle timeout)"

  parameter {
    name  = "maxmemory-policy"
    value = var.maxmemory_policy
  }

  parameter {
    name  = "notify-keyspace-events"
    value = ""
  }

  parameter {
    name  = "timeout"
    value = tostring(var.client_idle_timeout_seconds)
  }

  tags = merge(local.common_tags, {
    Name = local.parameter_group_name
  })
}

###############################################################################
# AUTH token
#
# Random 32-character ASCII string. ElastiCache Redis AUTH tokens must be
# 16–128 printable ASCII characters; `random_password` with `special = false`
# stays inside that envelope and avoids characters Redis rejects (`@`, `"`,
# `/`). transit_encryption_enabled = true makes auth_token mandatory.
###############################################################################

resource "random_password" "auth_token" {
  length  = 32
  special = false
  upper   = true
  lower   = true
  numeric = true
}

###############################################################################
# Replication group (the actual Redis cluster)
#
# - Engine pinned to redis 7.1; minor-version drift allowed because the
#   parameter-group family is `redis7` and `apply_immediately = true`.
# - Defaults: 2 nodes (1 primary + 1 replica), automatic failover on,
#   Multi-AZ on. Production scale-up changes node_type, not topology.
# - Encryption: KMS CMK (this module's `aws_kms_key.this`) at rest, TLS in
#   transit. Both required to use AUTH on Redis 7+.
# - Snapshots: snapshot_retention_limit days, snapshot_window UTC.
###############################################################################

resource "aws_elasticache_replication_group" "this" {
  replication_group_id = local.replication_group_id
  description          = "Allen BioData Registry 4-tier cache (API / NL / Schema / Access_Filter)"

  engine         = "redis"
  engine_version = var.engine_version
  node_type      = var.node_type
  port           = var.port

  num_cache_clusters         = var.num_cache_clusters
  automatic_failover_enabled = var.automatic_failover_enabled
  multi_az_enabled           = var.multi_az_enabled

  subnet_group_name    = aws_elasticache_subnet_group.this.name
  security_group_ids   = var.security_group_ids
  parameter_group_name = aws_elasticache_parameter_group.this.name

  # Encryption + auth.
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  kms_key_id                 = aws_kms_key.this.arn
  auth_token                 = random_password.auth_token.result
  auth_token_update_strategy = "ROTATE"

  # Backups.
  snapshot_retention_limit = var.snapshot_retention_limit
  snapshot_window          = var.snapshot_window

  # Maintenance.
  maintenance_window = var.maintenance_window

  apply_immediately = var.apply_immediately

  tags = merge(local.common_tags, {
    Name = local.replication_group_id
  })

  lifecycle {
    # `auth_token` rotates via auth_token_update_strategy; ignore drift on
    # the literal value so plan stays idempotent across rotations performed
    # outside Terraform (e.g. via the AWS console or a future rotation
    # Lambda).
    ignore_changes = [auth_token]
  }
}

###############################################################################
# Secrets Manager — AUTH token + endpoint metadata
#
# Stored as JSON `{auth_token, host, port}` so consuming Lambdas can fetch
# everything they need to construct a Redis URL with one GetSecretValue
# call. KMS-encrypted with this module's CMK (R31.3).
###############################################################################

resource "aws_secretsmanager_secret" "redis_auth" {
  name                    = local.auth_secret_name
  description             = "Redis AUTH token + endpoint metadata for ${local.replication_group_id}"
  kms_key_id              = aws_kms_key.this.arn
  recovery_window_in_days = var.auth_token_secret_recovery_window_days

  tags = merge(local.common_tags, {
    Name = local.auth_secret_name
  })
}

resource "aws_secretsmanager_secret_version" "redis_auth" {
  secret_id = aws_secretsmanager_secret.redis_auth.id
  secret_string = jsonencode({
    auth_token = random_password.auth_token.result
    host       = aws_elasticache_replication_group.this.primary_endpoint_address
    port       = aws_elasticache_replication_group.this.port
  })
}

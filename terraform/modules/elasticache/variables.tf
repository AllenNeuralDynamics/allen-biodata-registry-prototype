###############################################################################
# Variables — elasticache module
#
# ElastiCache for Redis cluster backing the Allen BioData Registry's four
# logical cache tiers (R20.1):
#
#   * API_Cache             5-minute TTL
#   * NL_Cache              30-minute TTL
#   * Schema_Cache          24-hour TTL
#   * Access_Filter_Cache   5-minute TTL
#
# Redis has no SQL-style "namespaces" — tier separation is enforced in the
# application layer (shared Lambda Layer, Task 12.1) by key prefix. This
# module provisions ONE replication group; the prefixes (`api:`, `nl:`,
# `schema:`, `access:`) live in the Lambda code that talks to it. See the
# README for the full key schema.
#
# Defaults are tuned for the PoC (cheapest 2-node footprint with HA failover,
# ~$26/mo). Production overrides live in the consuming environment
# composition.
###############################################################################

variable "name_prefix" {
  description = "Prefix applied to every resource name. Typically '<project>-<environment>', e.g. 'biodata-registry-dev'."
  type        = string
  default     = "biodata-registry-dev"

  validation {
    # Replication group IDs cap at 40 chars; we append "-redis" (6 chars), so
    # the prefix itself must be ≤ 34. Use 32 to leave a little headroom.
    condition     = length(var.name_prefix) > 0 && length(var.name_prefix) <= 32
    error_message = "name_prefix must be 1–32 characters (replication group IDs cap at 40 and we append '-redis')."
  }
}

variable "environment" {
  description = "Environment tag applied to every resource (dev, staging, prod)."
  type        = string
  default     = "dev"
}

variable "project" {
  description = "Project tag applied to every resource."
  type        = string
  default     = "biodata-registry"
}

variable "vpc_id" {
  description = "ID of the VPC the cluster is launched in. Sourced from the vpc module's `vpc_id` output. Currently unused by the resource graph (the subnet group + SGs imply VPC membership) but kept on the input contract for symmetry with sibling modules and to anchor future VPC-scoped resources (parameter store entries, alarms)."
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnet IDs the Redis replication group is attached to. Typically the vpc module's `private_subnet_ids` output. Multi-AZ HA (the default for this module) requires subnets in ≥ 2 AZs."
  type        = list(string)

  validation {
    condition     = length(var.private_subnet_ids) >= 2
    error_message = "Provide at least two private subnet IDs in distinct AZs (Multi-AZ HA is the default)."
  }
}

variable "security_group_ids" {
  description = "Security group IDs attached to the Redis replication group. Typically [vpc.internal_security_group_id] so VPC-bound Lambdas can reach Redis on port 6379."
  type        = list(string)

  validation {
    condition     = length(var.security_group_ids) >= 1
    error_message = "Provide at least one security group ID."
  }
}

variable "node_type" {
  description = "Cache node instance type. Default cache.t4g.micro is the cheapest Graviton2 node (~$13/mo per node, 0.5 GiB memory), sufficient for PoC working-set sizes (4 cache tiers, mostly small JSON values, target hit rate >80%). Production should size by working set + IOPS — cache.r7g.large is a reasonable starting point."
  type        = string
  default     = "cache.t4g.micro"
}

variable "engine_version" {
  description = "Redis engine version. Default 7.1 matches the parameter group family `redis7`. Newer minor versions are picked up automatically because `apply_immediately = true` and the parameter group is family-pinned, not version-pinned."
  type        = string
  default     = "7.1"
}

variable "port" {
  description = "Redis listener port. Default 6379. Override only if a corporate policy requires a non-standard port."
  type        = number
  default     = 6379

  validation {
    condition     = var.port > 0 && var.port < 65536
    error_message = "port must be a valid TCP port (1–65535)."
  }
}

variable "num_cache_clusters" {
  description = "Number of cache clusters in the replication group. PoC default 2 (1 primary + 1 replica) so automatic failover can promote the replica on primary loss. Production HA is the same shape; scale by node_type and (optionally) shard count, not cluster count, for cluster-mode-disabled deployments."
  type        = number
  default     = 2

  validation {
    condition     = var.num_cache_clusters >= 1 && var.num_cache_clusters <= 6
    error_message = "num_cache_clusters must be between 1 and 6."
  }
}

variable "automatic_failover_enabled" {
  description = "Enable automatic failover. PoC default true (paired with num_cache_clusters = 2). Required when multi_az_enabled = true."
  type        = bool
  default     = true
}

variable "multi_az_enabled" {
  description = "Enable Multi-AZ. PoC default true (subnets must span ≥ 2 AZs). Requires automatic_failover_enabled = true and num_cache_clusters >= 2."
  type        = bool
  default     = true
}

variable "snapshot_retention_limit" {
  description = "Number of days to retain Redis snapshots. PoC default 5 — the cache is rebuildable from Aurora on miss, so snapshots are a convenience, not a durability requirement; 5 days gives enough history to recover from operator error without significant cost. Set to 0 to disable snapshots entirely."
  type        = number
  default     = 5

  validation {
    condition     = var.snapshot_retention_limit >= 0 && var.snapshot_retention_limit <= 35
    error_message = "snapshot_retention_limit must be between 0 and 35."
  }
}

variable "snapshot_window" {
  description = "Daily time range during which automated snapshots are taken (UTC, format `HH:MM-HH:MM`). Default 07:00-08:00 UTC = midnight Pacific = registry quiet hours. Only used when snapshot_retention_limit > 0."
  type        = string
  default     = "07:00-08:00"
}

variable "maintenance_window" {
  description = "Weekly time range when AWS-driven maintenance can occur (UTC, format `ddd:HH:MM-ddd:HH:MM`). Default Sunday 09:00-10:00 UTC = Saturday 02:00-03:00 PT. Failover during this window is the normal expectation; Lambdas tolerate Redis loss (R20.7)."
  type        = string
  default     = "sun:09:00-sun:10:00"
}

variable "maxmemory_policy" {
  description = "Redis maxmemory eviction policy. Default `allkeys-lru` evicts the least-recently-used key when memory fills — the right choice for a multi-tier cache where every key is independently re-derivable from Aurora. TTL handles correctness (the design's per-tier TTLs); LRU handles capacity. Override only if a tier needs different eviction (it doesn't, for the PoC)."
  type        = string
  default     = "allkeys-lru"

  validation {
    condition = contains(
      [
        "noeviction",
        "allkeys-lru",
        "allkeys-lfu",
        "allkeys-random",
        "volatile-lru",
        "volatile-lfu",
        "volatile-random",
        "volatile-ttl",
      ],
      var.maxmemory_policy,
    )
    error_message = "maxmemory_policy must be one of the standard Redis eviction policies."
  }
}

variable "client_idle_timeout_seconds" {
  description = "Redis `timeout` parameter — close client connections idle for more than this many seconds. Default 300 (5 min). Lambda containers can outlive a single warm invocation, so an idle timeout protects against connection leaks; 300s is comfortably longer than the typical Lambda warm window. Set to 0 to disable."
  type        = number
  default     = 300

  validation {
    condition     = var.client_idle_timeout_seconds >= 0
    error_message = "client_idle_timeout_seconds must be ≥ 0 (0 disables the timeout)."
  }
}

variable "apply_immediately" {
  description = "Apply parameter, version, and node-type changes immediately rather than at the next maintenance window. PoC default true so iteration is fast; production may prefer false."
  type        = bool
  default     = true
}

variable "auth_token_secret_recovery_window_days" {
  description = "Recovery window in days for the AUTH-token Secrets Manager secret on delete. Default 7 (Secrets Manager minimum 7, max 30); set to 0 to delete immediately (use only in dev tear-down)."
  type        = number
  default     = 7

  validation {
    condition     = var.auth_token_secret_recovery_window_days == 0 || (var.auth_token_secret_recovery_window_days >= 7 && var.auth_token_secret_recovery_window_days <= 30)
    error_message = "auth_token_secret_recovery_window_days must be 0 (immediate delete) or between 7 and 30."
  }
}

variable "tags" {
  description = "Additional tags merged onto every resource. Project / Environment / Module are added automatically."
  type        = map(string)
  default     = {}
}

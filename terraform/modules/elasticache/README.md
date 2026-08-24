# `elasticache` Terraform module — Allen BioData Registry PoC

Provisions an ElastiCache for Redis 7.1 replication group that backs the
registry's **four logical cache tiers** (R20.1). The module ships:

- one Redis replication group (1 primary + 1 replica by default, Multi-AZ
  failover),
- one parameter group (`redis7` family, `allkeys-lru` eviction, 5-min
  client idle timeout),
- one subnet group spanning the VPC's private subnets,
- one dedicated KMS CMK (key rotation enabled),
- one Secrets Manager secret holding `{auth_token, host, port}`.

**Validates:** R20.1, R31.3, R32.2.
**Design references:** §Data Models.ElastiCache Redis Key Schema,
§IaC.Terraform Modules (`elasticache`).

---

## What this module does — and what it deliberately does not

Redis has no SQL-style "namespaces". The four cache tiers are
**key-prefix conventions enforced in the application layer**, not separate
Redis databases. This module:

- Provisions exactly ONE replication group shared by all four tiers.
- Configures sane LRU eviction (`allkeys-lru`) so the tier with the most
  active key set crowds out the others — desirable, because the cache is
  rebuildable from Aurora on miss.
- Wires KMS at-rest encryption, TLS in-transit encryption, and AUTH-token
  authentication.
- Does **not** create per-tier ACLs, per-tier Redis databases, or per-tier
  parameter groups. Tier separation lives in the shared Lambda Layer
  (Task 12.1), which is the only writer that talks to Redis. Reviewers
  should not expect Terraform-level tier creation — there is none.

## 4-tier key schema (application contract — owned by the Lambda Layer)

| Tier | Key pattern | Value | TTL | Invalidation |
|------|-------------|-------|-----|--------------|
| `API_Cache` | `api:{method}:{path}:{query_hash}:{user_scope_hash}` | JSON response body | 5 min | TTL only |
| `NL_Cache` | `nl:{sha256(normalized_query)}` | `{sql, result_ids}` | 30 min | TTL only |
| `Schema_Cache` | `schema:{org_id\|_biodata}:{name}:{version}` | JSON schema | 24 h | Explicit bust on schema version publish |
| `Access_Filter_Cache` | `access:{user_id}` | `{space_ids, roles}` | 5 min | Explicit bust on role / sharing-grant change |

The `API_Cache` `user_scope_hash` is derived from `{org_ids, space_ids,
roles}`, so when `Access_Filter_Cache` is busted, subsequent reads compute
a different scope hash and miss `API_Cache` as a side effect — no separate
`API_Cache` bust is required.

**TTL handling.** Redis 7 supports per-key TTL natively (`SET ... EX
<seconds>` or `EXPIRE`), so the per-tier TTLs in the design are encoded in
the Lambda code that writes each key. No Terraform-level configuration is
needed for TTLs.

## Architecture choices

- **Engine.** Redis 7.1 on the `redis7` parameter-group family. Newer
  minor versions are picked up automatically (`apply_immediately = true`,
  family-pinned parameter group).
- **Topology.** 1 primary + 1 replica with automatic failover and
  Multi-AZ (~30s typical promotion time on primary loss). Production
  scale-up changes `node_type`, not topology.
- **Node type.** `cache.t4g.micro` Graviton2 (~$13/mo per node, 0.5 GiB
  memory). Sufficient for PoC working sets (4 cache tiers, mostly small
  JSON values, target hit rate >80%); production should size by working
  set + IOPS — `cache.r7g.large` is a reasonable starting point.
- **At-rest encryption.** Customer-managed KMS CMK provisioned by this
  module, key rotation enabled. Required by R31.3.
- **In-transit encryption.** TLS, enabled (required to use AUTH on Redis
  7+).
- **Authentication.** `random_password`-generated AUTH token stored in
  Secrets Manager as `{name_prefix}-redis-auth-token` with
  `secret_string = {auth_token, host, port}` so consuming Lambdas resolve
  everything in one `GetSecretValue` call.
- **Eviction.** `allkeys-lru` — when memory fills, evict the
  least-recently-used key. TTL governs correctness (the design's per-tier
  TTLs); LRU governs capacity. LRU eviction kicks in only at memory
  exhaustion.
- **Idle timeout.** `timeout = 300` (5 min). Closes leaked client
  connections; Lambda warm windows are shorter so functional traffic is
  unaffected.
- **Keyspace notifications.** Disabled (`notify-keyspace-events = ""`).
  PoC Lambdas don't subscribe to keyspace events; documenting the choice
  prevents accidental enablement, which is expensive at scale.
- **Snapshots.** Retention = 5 days, snapshot window 07:00-08:00 UTC
  (registry quiet hours). The cache is rebuildable from Aurora on miss,
  so snapshots are a convenience for fast recovery from operator error,
  not a durability requirement.
- **Maintenance window.** Sunday 09:00-10:00 UTC = Saturday 02:00-03:00
  PT. AWS-driven failovers during this window are normal; Lambdas
  tolerate Redis loss (R20.7).

## Cost (PoC defaults)

| Item | Approx monthly cost (us-west-2) |
|------|---------------------------------|
| `cache.t4g.micro` × 2 (primary + replica, 24×7) | ~$26 |
| Backup storage (5-day retention) | ~$0.01/GB-hr (negligible at PoC working-set size) |
| Secrets Manager secret | $0.40 + API calls |
| KMS (CMK + decrypt API calls) | ≤ $1 (negligible at PoC volume) |
| **Total** | **~$28/mo** |

Production HA (3-shard, 6-node Multi-AZ on `cache.r7g.large`) would land
closer to $1 000+/mo — see HA trade-off below.

## HA model and operational expectations

The PoC defaults are already HA — this is a deliberate change from the
typical "single-node PoC" pattern, because R20.7 is the *only* fallback
path and we want the PoC to demonstrate the production failover behavior:

- `num_cache_clusters = 2` (1 primary + 1 replica)
- `automatic_failover_enabled = true`
- `multi_az_enabled = true` (subnets must span ≥ 2 AZs)
- `snapshot_retention_limit = 5` (5-day snapshots)

> **R20.7** — IF ElastiCache Redis is unavailable, THEN THE Lambda
> functions SHALL fall through to Aurora PostgreSQL directly without
> caching.

Multi-AZ failover takes **~30 seconds** end-to-end (replica promotion +
DNS swing). Lambdas should:

1. Use the **primary endpoint** (which is DNS-stable across failovers).
2. Set a connect timeout of 1–2 seconds and a single retry on connection
   failure to absorb the failover gap; the second attempt typically lands
   on the new primary cleanly.
3. On retry exhaustion, fall through to Aurora per R20.7.

Cache loss costs latency, not correctness.

Production scale-up changes `node_type` (and optionally enables cluster
mode); the topology defaults are already correct.

## Inputs

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `name_prefix` | `string` | `biodata-registry-dev` | Resource name prefix; ≤ 32 chars (we append `-redis`). |
| `environment` | `string` | `dev` | Environment tag. |
| `project` | `string` | `biodata-registry` | Project tag. |
| `vpc_id` | `string` | — (required) | VPC ID, sourced from the vpc module's `vpc_id` output. |
| `private_subnet_ids` | `list(string)` | — (required) | Private subnet IDs (≥ 2 AZs for Multi-AZ HA). |
| `security_group_ids` | `list(string)` | — (required) | SGs attached to the replication group; typically `[vpc.internal_security_group_id]`. |
| `node_type` | `string` | `cache.t4g.micro` | Cache node instance type. |
| `engine_version` | `string` | `7.1` | Redis engine version. |
| `port` | `number` | `6379` | Redis listener port. |
| `num_cache_clusters` | `number` | `2` | Number of cache clusters in the replication group. |
| `automatic_failover_enabled` | `bool` | `true` | Enable automatic failover. |
| `multi_az_enabled` | `bool` | `true` | Enable Multi-AZ. |
| `snapshot_retention_limit` | `number` | `5` | Days of snapshot retention. |
| `snapshot_window` | `string` | `07:00-08:00` | Daily snapshot window (UTC). |
| `maintenance_window` | `string` | `sun:09:00-sun:10:00` | Weekly maintenance window (UTC). |
| `maxmemory_policy` | `string` | `allkeys-lru` | Redis eviction policy. |
| `client_idle_timeout_seconds` | `number` | `300` | Redis `timeout` parameter. |
| `apply_immediately` | `bool` | `true` | Apply changes immediately rather than at maintenance window. |
| `auth_token_secret_recovery_window_days` | `number` | `7` | Recovery window for the AUTH-token secret on delete. |
| `tags` | `map(string)` | `{}` | Additional tags merged onto every resource. |

## Outputs

| Name | Description |
|------|-------------|
| `replication_group_id` | ID of the Redis replication group. |
| `replication_group_arn` | ARN — useful for IAM resource scoping. |
| `primary_endpoint` | Primary endpoint hostname (Lambdas connect here). |
| `reader_endpoint` | Reader endpoint (load-balanced across replicas). |
| `configuration_endpoint` | Configuration endpoint (cluster-mode-enabled only; empty otherwise). |
| `port` | Listener port (default 6379). |
| `engine_version_actual` | Actual running engine version. |
| `subnet_group_name` | Name of the ElastiCache subnet group. |
| `parameter_group_name` | Name of the ElastiCache parameter group. |
| `auth_token_secret_arn` | ARN of the Secrets Manager secret holding `{auth_token, host, port}`. |
| `auth_token_secret_name` | Name of the secret. |
| `kms_key_arn` | ARN of the dedicated CMK encrypting the cluster + secret. |
| `kms_key_alias` | KMS key alias for human-readable references. |
| `security_group_id` | First SG attached to the replication group. |

## How Lambdas consume this module

Lambda execution roles need:

- `secretsmanager:GetSecretValue` on `auth_token_secret_arn`.
- `kms:Decrypt` on `kms_key_arn` — Secrets Manager will request it during
  `GetSecretValue`.
- The shared Lambda Layer (Task 12.1) reads the secret once at cold start,
  constructs `rediss://default:{auth_token}@{host}:{port}` (note `rediss://`
  for TLS), and uses `redis-py` with `ssl=True`.

**Retry behavior.** The Lambda Layer should retry once on connection
failure to absorb the ~30s Multi-AZ failover gap; on retry exhaustion,
fall through to Aurora per R20.7.

## Authentication model

| Layer | Mechanism |
|-------|-----------|
| Network | Replication group sits in private subnets behind the `internal` SG; only VPC-bound Lambdas can reach it. |
| Transport | TLS in-transit encryption (`transit_encryption_enabled = true`). |
| Authentication | Redis AUTH token, 32-char ASCII random, stored in Secrets Manager. |
| At-rest | KMS encryption (dedicated CMK provisioned by this module). |

## Example usage (dev environment composition)

```hcl
module "elasticache" {
  source = "../../modules/elasticache"

  name_prefix = "biodata-registry-dev"
  environment = "dev"
  project     = "biodata-registry"

  vpc_id             = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids
  security_group_ids = [module.vpc.internal_security_group_id]

  tags = local.common_tags
}
```

## References

- Design: §Data Models.ElastiCache Redis Key Schema (`design.md`)
- Requirements: R20.1 (4 tiers), R20.7 (Redis-down fallthrough), R31.3
  (KMS at rest), R32.2 (single `terraform apply`)
- Tasks: 4.3 (this module), 12.1 (shared Lambda Layer that owns prefix
  discipline and per-tier TTLs), 26.2 (Property 13: Cache Invalidation
  Correctness)

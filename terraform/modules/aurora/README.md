# `aurora` Terraform module — Allen BioData Registry PoC

Provisions the source-of-truth database for the Allen BioData Registry: an
Aurora PostgreSQL Serverless v2 cluster with logical replication enabled
for CDC, the `pgvector`, `pgaudit`, and `pg_stat_statements` extensions
preloaded, KMS encryption at rest, IAM database authentication, a Secrets
Manager secret that the business Lambdas resolve at runtime, and a post-
apply bootstrap that creates the `vector` and `pg_trgm` extensions plus
the `biodata_cdc` logical replication slot used by the CDC pipeline.

**Validates:** R1.7 (Aurora is the canonical write target), R28.1, R28.2
(WAL-based logical replication powers the CDC pipeline), R31.1 (credentials
in Secrets Manager), R31.3 (KMS-at-rest), R32.2 (`terraform apply` provisions
Aurora as part of the stack).

**Design reference:** `design.md` §Architecture.CDC Pipeline Architecture and
§Infrastructure as Code.Terraform Modules (`aurora`).

---

## What this module creates

| Resource | Purpose |
|---|---|
| `aws_rds_cluster.this` | Aurora PostgreSQL 16.13 cluster, Serverless v2 scaling, KMS-encrypted storage, IAM DB auth enabled, postgresql logs to CloudWatch |
| `aws_rds_cluster_instance.this[*]` | One or more `db.serverless` instances. The first is the writer; any additional instances are readers. Performance Insights and Enhanced Monitoring (60s) are enabled by default |
| `aws_rds_cluster_parameter_group.this` | Cluster parameter group enabling `rds.logical_replication = 1`, `shared_preload_libraries = 'pg_stat_statements,pgaudit,vector'`, `log_statement = 'ddl'`, `max_replication_slots = 10`, `max_wal_senders = 10`, `wal_sender_timeout = 0`, `track_activity_query_size = 4096` |
| `aws_db_parameter_group.this` | Instance parameter group with `log_min_duration_statement = 1000` for slow-query logging |
| `aws_db_subnet_group.this` | Spans the private subnets exported by the `vpc` module |
| `aws_secretsmanager_secret.master` + `_version` | Holds `{username, password, host, port, dbname, engine, cdc_replication_slot}` — KMS-encrypted with the same CMK. Secret name: `<name_prefix>-aurora-master`. Rotation is intentionally disabled for the PoC; a commented `aws_secretsmanager_secret_rotation` block is included with a TODO |
| `aws_kms_key.aurora` (conditional) | Module-local CMK created only when `var.kms_key_arn` is null. Has key rotation enabled and a policy that grants RDS + Secrets Manager service principals usage |
| `aws_kms_alias.aurora` (conditional) | Friendly alias `alias/<name_prefix>-aurora` for the module-local CMK |
| `random_password.master` | 32-char password used for the master user; written to Secrets Manager and never echoed to outputs |
| `aws_iam_role.rds_monitoring` (conditional) | IAM role for RDS Enhanced Monitoring, with the AWS managed `AmazonRDSEnhancedMonitoringRole` policy attached |
| `null_resource.bootstrap_slot_and_extensions` | Post-apply provisioner that creates the `vector` and `pg_trgm` extensions and the `biodata_cdc` logical replication slot via `psql`. Idempotent (`IF NOT EXISTS` + `WHERE NOT EXISTS`). Re-runs only when the cluster endpoint, slot name, or secret version change |

The cluster is built with `engine_mode = "provisioned"` — that is the value
required for **Serverless v2**. (Serverless v1 used `"serverless"`; v2
overlays a `serverlessv2_scaling_configuration` block on a provisioned
cluster.)

---

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| `name_prefix` | string | `biodata-registry-dev` | Prefix for every resource name |
| `environment` | string | `dev` | Tag |
| `project` | string | `biodata-registry` | Tag |
| `vpc_id` | string | _(required)_ | VPC the cluster joins |
| `private_subnet_ids` | list(string) | _(required, ≥2)_ | Private subnets for the DB subnet group |
| `security_group_ids` | list(string) | _(required, ≥1)_ | SGs attached to the cluster's network interfaces (typically the `vpc` module's `internal_security_group_id`) |
| `kms_key_arn` | string | `null` | Optional external CMK. If null, the module creates one |
| `engine_version` | string | `16.13` | Latest stable 16.x in us-west-2. **Not** `-limitless` |
| `db_name` | string | `biodata_registry` | Initial database name (no hyphens) |
| `master_username` | string | `biodata_admin` | Master user; password is generated |
| `min_capacity_acu` | number | `0.5` | Serverless v2 floor — ~$43/mo at idle |
| `max_capacity_acu` | number | `4.0` | Serverless v2 ceiling — raise for production |
| `instance_count` | number | `1` | Writer + N-1 readers. PoC default 1 (writer only) |
| `backup_retention_days` | number | `7` | PoC default; production should use 30 |
| `preferred_backup_window` | string | `06:00-07:00` | UTC (≈ 22:00-23:00 PST) |
| `preferred_maintenance_window` | string | `sun:08:00-sun:09:00` | UTC (≈ Sun 00:00-01:00 PST) |
| `skip_final_snapshot` | bool | `true` | **PoC trade-off — production MUST set false** |
| `deletion_protection` | bool | `false` | **PoC trade-off — production MUST set true** |
| `secrets_recovery_window_days` | number | `7` | Secrets Manager recovery window. Production uses 30 |
| `cdc_replication_slot_name` | string | `biodata_cdc` | Slot name for the CDC pipeline (Task 17.1) |
| `iam_database_authentication_enabled` | bool | `true` | Enable IAM DB auth (free, doesn't conflict with master password auth) |
| `performance_insights_enabled` | bool | `true` | Enable Performance Insights on every instance |
| `monitoring_interval_seconds` | number | `60` | Enhanced Monitoring granularity (0, 1, 5, 10, 15, 30, 60) |
| `bootstrap_slot_via_null_resource` | bool | `true` | Run the slot/extension `null_resource` provisioner. Disable when running terraform from outside the VPC |
| `tags` | map(string) | `{}` | Additional tags |

---

## Outputs

| Name | Description |
|---|---|
| `cluster_id` | Cluster identifier (string) |
| `cluster_arn` | Cluster ARN |
| `cluster_resource_id` | Immutable `cluster-xxx` id — required for IAM database authentication |
| `cluster_endpoint` | Writer endpoint hostname |
| `cluster_reader_endpoint` | Read-only load-balanced endpoint |
| `db_name` | Initial database name |
| `port` | `5432` |
| `master_username` | Master username (sensitive) |
| `master_secret_arn` | Secrets Manager ARN — Lambdas resolve at runtime |
| `master_secret_name` | Secrets Manager secret name (`<name_prefix>-aurora-master`) |
| `master_secret_version_id` | Current secret version id (etag-like signal for rotation) |
| `kms_key_arn` | CMK used to encrypt storage + secret (external or module-created) |
| `kms_key_managed_by_module` | `true` if the module created the CMK |
| `parameter_group_name` | Custom cluster parameter group |
| `cluster_parameter_group_name` | Alias of `parameter_group_name` for clarity |
| `db_parameter_group_name` | Per-instance parameter group (slow-query logging) |
| `db_subnet_group_name` | DB subnet group |
| `security_group_ids` | Pass-through of the SGs attached to the cluster |
| `instance_identifiers` | List of cluster instance ids |
| `cdc_replication_slot_name` | Slot name (e.g. `biodata_cdc`) — passed through to the cdc-pipeline module |
| `replication_slot_name` | Alias of `cdc_replication_slot_name` |
| `iam_database_authentication_enabled` | Pass-through of the IAM DB auth toggle |
| `monitoring_role_arn` | ARN of the Enhanced Monitoring IAM role (null when disabled) |

Downstream consumers fetch credentials from Secrets Manager:

```python
import boto3, json, os, psycopg

secrets = boto3.client("secretsmanager")
cred = json.loads(secrets.get_secret_value(SecretId=os.environ["AURORA_SECRET_ARN"])["SecretString"])
conn = psycopg.connect(
    host=cred["host"], port=cred["port"], dbname=cred["dbname"],
    user=cred["username"], password=cred["password"], sslmode="require",
)
```

---

## Logical replication slot bootstrap

This module ships with a `null_resource` that runs `psql` against the
cluster after `terraform apply` to:

1. `CREATE EXTENSION IF NOT EXISTS vector;` — the pgvector extension. The
   library is preloaded via `shared_preload_libraries = 'pg_stat_statements,pgaudit,vector'`;
   `CREATE EXTENSION` activates it. Note: the extension name is **`vector`**, not `pgvector`.
2. `CREATE EXTENSION IF NOT EXISTS pg_trgm;` — trigram similarity, used by
   `Duplicates_Lambda`'s SQL `similarity()` checks.
3. `SELECT pg_create_logical_replication_slot('biodata_cdc', 'pgoutput')
   WHERE NOT EXISTS (...)` — the CDC pipeline (Task 17.1) consumes this slot.

The provisioner uses `triggers` keyed on `cluster_endpoint`,
`replication_slot_name`, the secret version id, and `cluster_resource_id`,
so it only re-runs when one of those changes. The SQL is itself idempotent
(`IF NOT EXISTS` + a guarded `pg_create_logical_replication_slot`) so a
re-run is safe.

### Operator prerequisites

* `psql` (the `libpq` client) on the operator's `PATH`.
  * macOS: `brew install libpq && brew link --force libpq`
  * Ubuntu: `apt-get install postgresql-client`
* Network reach from wherever Terraform runs to the cluster's writer
  endpoint over port 5432. Aurora is in private subnets, so the operator
  **MUST be inside the VPC**. Recommended options:
  * Run `terraform apply` from a Cloud9 workspace inside the VPC.
  * Run from an EC2 instance reached via SSM Session Manager.
  * AWS Client VPN.
  * Any equivalent that puts the executor on a route to the private
    subnets and through the VPC's security group.
* AWS credentials in the environment with `secretsmanager:GetSecretValue`
  on the master secret (the same credentials Terraform itself uses).
* `python3` (used to parse the JSON secret string in the provisioner).

### TODO for Task 10 — `terraform apply` end-to-end

If `terraform apply` is run from outside the VPC (for example, from a CI
runner without VPN), set `bootstrap_slot_via_null_resource = false` and
have one of these handle bootstrap instead:

1. **Migration runner (Task 8.1) — recommended.** Idempotent, runs the
   same SQL, lives alongside the rest of the schema migrations, and is
   already deployed to a Lambda inside the VPC. No additional code is
   needed.
2. **Lambda-backed bootstrapper.** A small Python Lambda invoked via
   `aws_lambda_invocation` that resolves the master secret, connects with
   `psycopg` over `sslmode=require`, and runs the same three SQL
   statements. More code than option 1 but keeps the bring-up entirely
   inside the Terraform graph and removes the operator-friction of
   needing VPC reach.

Defer the choice to the `envs/dev` environment composition (Task 10).

---

## PoC trade-offs

This module ships several defaults that make life easy for a short-lived
PoC. Each carries a comment in the Terraform; the consolidated list:

| Default | PoC value | Production value | Why |
|---|---|---|---|
| `skip_final_snapshot` | `true` | `false` | A 7 GB snapshot on tear-down is wasted spend during a 3-week PoC. **Flip before any environment with real data.** |
| `deletion_protection` | `false` | `true` | `terraform destroy` is the cleanest tear-down path during PoC iteration. Production must protect the cluster. |
| `backup_retention_days` | `7` | `30` | RPO is shorter for the PoC because no users depend on the cluster yet. |
| `secrets_recovery_window_days` | `7` | `30` | Same reasoning — short window for PoC iteration. |
| `min_capacity_acu` / `max_capacity_acu` | `0.5` / `4.0` | tune to load (e.g. `1.0` / `16.0`) | PoC traffic is intermittent and a single ACU has plenty of headroom. |
| `instance_count` | `1` | ≥ 2 | Writer-only is fine for a PoC; production wants a reader for fail-over and Search_Lambda fallback. |
| Secrets Manager rotation | disabled (commented) | enabled with AWS-managed RDS rotation Lambda | Master password rotates out-of-band on a 30-day schedule in production. |
| `apply_immediately` | `true` | `false` | PoC iteration speed; production should coordinate disruptive parameter changes through the maintenance window. |

---

## Cost note (us-west-2, eyeball)

At the PoC defaults the cluster's idle cost is dominated by the Serverless
v2 floor:

* **Compute idle:** 0.5 ACU × 24 h × 30 d × $0.12/ACU-hr ≈ **$43/mo**.
* **Compute active (sustained 4.0 ACU):** 4.0 ACU × 24 h × 30 d × $0.12/ACU-hr ≈ **$345/mo**.
  Real PoC traffic will sit close to the idle floor; the active number is
  the worst-case ceiling at `max_capacity_acu = 4.0`.
* **Storage:** $0.10/GB-month for cluster storage and $0.20/M IOPS — for a
  PoC dataset under ~10 GB, well under $5/mo.
* **Backup:** Free up to the cluster size for the first 7 days of
  retention; beyond that, $0.021/GB-month.
* **Performance Insights:** Free for 7 days of retention.
* **Enhanced Monitoring (60s):** ~$0.01/instance/hr → ~$7/instance/month.
* **CloudWatch Logs (`postgresql` export):** scales with DDL volume —
  negligible during PoC.
* **KMS CMK:** $1/month for the module-created CMK; usage charges are
  rounding error.
* **Secrets Manager:** $0.40/secret/month + $0.05 per 10K API calls.

Total expected: **~$50-$60/mo** when idle, growing modestly with active
query traffic and storage.

---

## How to consume

```hcl
module "aurora" {
  source = "../modules/aurora"

  name_prefix = "biodata-registry-dev"
  environment = "dev"
  project     = "biodata-registry"

  vpc_id             = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids
  security_group_ids = [module.vpc.internal_security_group_id]

  # Optional — supply your own CMK if the central security team owns key
  # lifecycle. Leave null and the module creates one.
  # kms_key_arn = module.kms.cmk_arn

  # When running `terraform apply` from outside the VPC (e.g. CI without
  # VPN), disable the local-exec bootstrap and have the migration runner
  # or a Lambda-backed bootstrapper create the slot + extensions instead.
  # bootstrap_slot_via_null_resource = false

  tags = {
    Owner = "biodata-registry-team"
  }
}
```

After apply, downstream Lambdas fetch credentials from Secrets Manager
(see the snippet above). The CDC pipeline (Task 17.1) uses
`module.aurora.cluster_endpoint`, `module.aurora.replication_slot_name`,
and `module.aurora.master_secret_arn` to attach to the slot.

# `documentdb` Terraform module — Allen BioData Registry PoC

Provisions Amazon DocumentDB as the **MongoDB-compatible read layer** fed by
the CDC pipeline (Aurora WAL → Indexing_Lambda → DocumentDB). Preserves the
shape `aind-data-access-api` already speaks so the Allen Institute's existing
tooling keeps working unchanged.

**Validates:** R28.4 (DocumentDB read layer for `aind-data-access-api`),
R31.3 (KMS encryption at rest), R32.2 (`terraform apply` provisions
DocumentDB).

**Design references:**

* `design.md` §Data Models.DocumentDB Document Shape
* `design.md` §Design Decisions.DocumentDB Access Model and Trust Boundary

---

## What this module provisions

| Resource | Purpose |
|---|---|
| `aws_kms_key` (optional) | Dedicated CMK for storage + Secrets at rest. Skipped if `var.kms_key_arn` is supplied. R31.3. |
| `aws_kms_alias` (optional) | `alias/<name_prefix>-documentdb` for the module-owned CMK. |
| `aws_docdb_subnet_group` | DB subnet group spanning the private subnets exported by the `vpc` module (VPC-only — no public access). |
| `aws_docdb_cluster_parameter_group` | Family `docdb5.0`. Sets `audit_logs = enabled` (production-hardening path) and `tls = enabled` (default in 5.0; set explicitly for review). |
| `aws_docdb_cluster` | Engine `docdb 5.0.0` (MongoDB 5.0 wire protocol). KMS-encrypted, 7-day backup retention, audit + profiler logs to CloudWatch. |
| `aws_docdb_cluster_instance` × N | Default `1 × db.r6g.large` for the PoC. |
| `random_password` × 2 | 32-char master + read-only passwords (DocDB-safe alphabet, generated up-front so the read-only secret is real on first apply). |
| `aws_secretsmanager_secret` × 2 | `{name_prefix}-documentdb-master` and `{name_prefix}-documentdb-readonly`. KMS-encrypted, 7-day recovery window. |
| `null_resource.readonly_user_bootstrap` | Runs `mongosh` after the cluster is up, creates (or refreshes) the `biodata_reader` DB user with the `readAnyDatabase` role. |

Cluster identifier: `${var.name_prefix}-documentdb`.

---

## Engine version & wire protocol

This module pins `engine_version = "5.0.0"`. DocumentDB 5.0 exposes the
MongoDB 5.0 wire protocol, which is what `aind-data-access-api` is tested
against. Two consequences worth flagging:

1. **TLS is the default** in DocumentDB 5.0. Clients MUST connect with
   `tls=true` and verify against the AWS-published CA bundle. The bundle
   is exported by this module as `tls_ca_bundle_url` — the URL is
   <https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem>.
   The cert is not bundled with the cluster; it must be fetched out-of-band
   and shipped with the consumer (Lambda layer, container image, EC2 AMI).

2. **DocumentDB 5.0 dropped the `db.t3.medium` class** that the cheaper 4.0
   PoCs used. The smallest current-generation class supported on 5.0 is
   `db.r6g.large` — that is the module default. Cost section below.

---

## The "IAM database authentication" caveat (read this first)

The design document asks for **"IAM database authentication"** on
DocumentDB. Native IAM database authentication in the *RDS sense* —
short-lived auth tokens minted via `aws rds generate-db-auth-token` — **is
not supported by DocumentDB**. DocumentDB authenticates with a SCRAM
username + password exchanged at the MongoDB wire level.

The trust boundary the design relies on is therefore composed of five
independent controls (see `design.md` §Design Decisions.DocumentDB Access
Model and Trust Boundary):

1. **VPC isolation** — the cluster has no public endpoint and is reachable
   only from inside the Allen Institute VPC. `vpc_security_group_ids`
   restricts the network surface to members of the `internal` SG.
2. **IAM-protected master credential** — the master password lives in
   Secrets Manager (`master_secret_arn`). `secretsmanager:GetSecretValue`
   on that ARN is the IAM gate that determines who can mint a privileged
   DocDB connection.
3. **TLS in transit** — DocumentDB 5.0 enables TLS by default; the
   parameter group sets `tls = enabled` explicitly so the value is
   reviewable. Clients verify against `global-bundle.pem`.
4. **A read-only DB user** — `aind-data-access-api` consumers connect as
   `biodata_reader` with the Mongo `readAnyDatabase` role. The role
   disallows `insert`, `update`, `delete`, `createIndex`, etc. — exactly
   the read-only contract the design relies on. The read-only credentials
   live in a separate Secrets Manager secret (`readonly_secret_arn`) so
   consumer credentials can rotate without touching the master.
5. **Client-library RLS-equivalent filtering** — every query carries
   `{space_id: {$in: user.visible_space_ids}, is_sensitive: false, ...}`.
   This filter is the application-layer half of the boundary and is
   enforced by the `aind-data-access-api` client library, not by
   DocumentDB.

A malicious VPC insider with master-secret access can bypass the read-only
contract — that risk is documented in `design.md` and accepted for the
PoC. Production hardening relies on the audit logs (already on, by
parameter group) plus a per-query review pipeline.

> **What "IAM database authentication" means in the Allen design**: IAM
> protects who can fetch the credentials, the network protects who can
> reach the cluster at all, and the read-only DB user limits what a
> connected client can do. The composition is the trust boundary — there
> is no native per-request IAM check on DocumentDB.

> **Subtlety (lifted from `design.md`)**: IAM authenticates the
> *connection*, not the *individual request*. End-user identity (the
> visible `space_ids`, `org_ids`, `is_sensitive` flag) is carried *by the
> client library* into every query's filter clause. Without that
> client-side filter, a connected client sees all denormalized
> documents — including sensitive ones.

---

## How `aind-data-access-api` consumers connect

After `terraform apply` has produced both Secrets Manager secrets and the
read-only-user bootstrap has populated `biodata_reader`, a Python consumer
running inside the Allen Institute VPC connects as follows:

```python
# requirements: pymongo>=4.6, boto3>=1.34, cryptography>=42
import json
import os
import urllib.request
from urllib.parse import quote_plus

import boto3
from pymongo import MongoClient

REGION = "us-west-2"
READONLY_SECRET_ARN = os.environ["DOCDB_READONLY_SECRET_ARN"]

# Step 1 — fetch the read-only credential from Secrets Manager
sm = boto3.client("secretsmanager", region_name=REGION)
secret = json.loads(
    sm.get_secret_value(SecretId=READONLY_SECRET_ARN)["SecretString"]
)

# Step 2 — make sure the CA bundle is on disk (bake into Lambda layer in prod)
ca_path = "/tmp/global-bundle.pem"
if not os.path.exists(ca_path):
    urllib.request.urlretrieve(secret["sslCABundleUrl"], ca_path)

# Step 3 — connect over TLS using the reader endpoint (load-balanced)
uri = (
    f"mongodb://{quote_plus(secret['username'])}:{quote_plus(secret['password'])}"
    f"@{secret['reader_host']}:{secret['port']}/"
    "?tls=true"
    "&retryWrites=false"
    "&replicaSet=rs0"
    "&readPreference=secondaryPreferred"
)
client = MongoClient(uri, tlsCAFile=ca_path)

# Step 4 — apply the RLS-equivalent client-side filter (R8.4 / R17.9).
# The Indexing_Lambda writes denormalized space_id / org_id / is_sensitive
# onto every document. The client library MUST add these to every query.
db = client[secret["dbName"]]
visible_space_ids = ["space-1234", "space-5678"]  # from caller's auth context
results = db.data_asset.find({
    "space_id":     {"$in": visible_space_ids},
    "is_sensitive": False,
    # ... your additional query criteria ...
})
for doc in results:
    print(doc["_id"], doc["name"])
```

Key things the consumer is responsible for:

* **Downloading the CA bundle** — fetch
  `https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem`
  (output as `tls_ca_bundle_url`) and reference it with `tlsCAFile=...`.
  Bake it into the Lambda layer / EC2 AMI / container image so it's
  present at cold-start time.
* **Carrying end-user identity** — the connection authenticates as the
  service principal of whatever invoked it, not as the human user. The
  client library is responsible for constructing the
  `{space_id: {$in: ...}, is_sensitive: false}` filter on every request.
  This is the application-layer half of the RLS-equivalent boundary;
  without it, DocumentDB returns all documents the service can see.
* **Using the `reader_host` for reads.** The cluster endpoint
  (`host`) is the writer; the reader endpoint load-balances across the
  available replicas. PoC has only one instance, so both endpoints point
  at the same node — but consumers that follow this convention will pick
  up replicas automatically when the cluster is scaled up.

The consumer-facing version of this guide will live at
`docs/docdb-access.md` (Task 18.3) and is the canonical reference for
external `aind-data-access-api` integrations.

---

## Read-only DB user bootstrap

DocumentDB has no Terraform-native "user" resource — Mongo users live
inside the cluster and are created via the wire protocol. This module
includes a `null_resource` + `local-exec` that runs `mongosh` after the
cluster is up and creates (or refreshes) `biodata_reader` with the
`readAnyDatabase` role.

### Operator pre-requisites

Same caveat as the Aurora bootstrap (Task 3.1):

1. **mongosh installed** on PATH (or set `var.mongosh_binary` explicitly).
   On macOS: `brew install mongosh`. On Linux:
   `sudo apt install mongodb-mongosh` or download the `.deb` from MongoDB.
2. **VPC reach** to the cluster — the operator must be on the Allen
   Institute VPN, a bastion, or running terraform from inside the VPC
   (e.g. via SSM session manager). The cluster has no public endpoint.
3. **`curl` on PATH** so the bootstrap can fetch the CA bundle to a temp
   file. Standard on every dev workstation.

If mongosh or VPC reach is unavailable, set
`enable_readonly_user_bootstrap = false` and run the equivalent
mongosh script manually post-apply (the inline script in `main.tf` is the
template).

### What the bootstrap does

1. Downloads the CA bundle to a `mktemp`-allocated file.
2. Connects to the cluster as the master user over TLS.
3. Runs `db.adminCommand('createUser', ...)` for `biodata_reader` with
   the password Terraform generated and stored in `readonly_secret_arn`.
4. On retry / re-apply, catches the "user already exists" error
   (DocumentDB code 51003) and runs `updateUser` instead so the
   bootstrap is idempotent.

### What it does NOT do

* It does not rotate the password on a schedule. To rotate, taint the
  `random_password.readonly` resource and re-apply.
* It does not restrict the user to a single database. The role is
  `readAnyDatabase` so the consumer can also see admin/system collections
  for diagnostics. Tightening to a per-database `read` role on
  `biodata_registry` only is a production-hardening item.
* It does not run from inside the VPC. Production should replace the
  local-exec with a one-shot Lambda triggered by Terraform (handed to
  Task 8 follow-up).

---

## PoC trade-offs (documented for QC1 review)

| Decision | PoC | Production target |
|---|---|---|
| Instance count | **1** (no failover) | ≥2 across AZs |
| Instance class | `db.r6g.large` (smallest 5.0-supported) | `db.r6g.xlarge` or larger |
| Backup retention | 7 days | 30 days |
| Backup window | 07:00–08:00 UTC | tune to org's low-traffic window |
| Maintenance window | sun:09:00–sun:10:00 UTC | tune |
| `skip_final_snapshot` | `true` (`terraform destroy` deletes data) | `false` |
| `apply_immediately` | `true` (immediate parameter changes) | `false` (use maintenance window) |
| `deletion_protection` | `false` | `true` |
| Native RDS-style IAM auth | **not natively supported by DocumentDB** | Audit-log-based per-query review pipeline (production hardening path) |
| TLS | enabled (default in 5.0; set explicitly in parameter group) | same |
| Read-only user role | `readAnyDatabase` | `read` scoped to `biodata_registry` only |
| Read-only-user bootstrap | `mongosh` from operator workstation | one-shot Lambda inside the VPC |

### Cost (us-west-2, on-demand, July 2026 list pricing)

DocumentDB has no Serverless option for v5; the smallest production-class
instance is required.

| Item | Cost |
|---|---|
| 1× `db.r6g.large` (24 × 30 × $0.277/hr) | ~$200/mo |
| Storage (10 GB at PoC sample size) | ~$1/mo |
| I/O (10M ops/mo at PoC throughput) | ~$2/mo |
| Backup (~10 GB × 7 days, 1× free tier) | ~$1/mo |
| Audit + profiler CloudWatch Logs (~1 GB/mo) | ~$1/mo |
| KMS CMK (key + ~10k requests) | <$1/mo |
| Secrets Manager (2 secrets × $0.40) | $0.80/mo |
| **Total PoC baseline** | **~$210/mo** |

Production HA with two `db.r6g.large` instances across AZs runs roughly
**$415/mo** before storage / I/O scaling. Larger instance classes scale
linearly (e.g., `db.r6g.xlarge` ≈ 2×).

---

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| `name_prefix` | `string` | _(required)_ | Resource name prefix. |
| `environment` | `string` | `"dev"` | Environment tag. |
| `project` | `string` | `"biodata-registry"` | Project tag. |
| `vpc_id` | `string` | _(required)_ | From `module.vpc.vpc_id`. |
| `private_subnet_ids` | `list(string)` | _(required, ≥2 distinct AZs)_ | From `module.vpc.private_subnet_ids`. |
| `security_group_ids` | `list(string)` | _(required)_ | Typically `[module.vpc.internal_security_group_id]`. |
| `kms_key_arn` | `string` | `null` | Pre-existing KMS CMK; module creates one if null. |
| `db_name` | `string` | `"biodata_registry"` | Logical database name. |
| `master_username` | `string` | `"docdb_admin"` | Master DB username. |
| `instance_class` | `string` | `"db.r6g.large"` | DocumentDB instance class. |
| `instance_count` | `number` | `1` | Number of cluster instances. |
| `engine_version` | `string` | `"5.0.0"` | DocumentDB engine version. |
| `backup_retention_period` | `number` | `7` | Days to retain automated backups. |
| `preferred_backup_window` | `string` | `"07:00-08:00"` | Daily backup window (UTC). |
| `preferred_maintenance_window` | `string` | `"sun:09:00-sun:10:00"` | Weekly maintenance window (UTC). |
| `skip_final_snapshot` | `bool` | `true` | PoC convenience. |
| `deletion_protection` | `bool` | `false` | PoC convenience; flip in production. |
| `apply_immediately` | `bool` | `true` | PoC convenience. |
| `secret_recovery_window_in_days` | `number` | `7` | Secrets Manager soft-delete window. |
| `enabled_cloudwatch_log_exports` | `list(string)` | `["audit", "profiler"]` | DocDB log types. |
| `readonly_username` | `string` | `"biodata_reader"` | Read-only DB user. |
| `enable_readonly_user_bootstrap` | `bool` | `true` | Run the mongosh local-exec to create the read-only user. |
| `mongosh_binary` | `string` | `"mongosh"` | Path/name of mongosh on the operator workstation. |
| `tls_ca_bundle_url` | `string` | RDS truststore | Override the CA bundle URL only if you mirror it internally. |
| `tags` | `map(string)` | `{}` | Extra tags. |

## Outputs

| Name | Description |
|---|---|
| `cluster_endpoint` | Writer endpoint. |
| `reader_endpoint` | Reader endpoint. The aind-data-access-api consumers should connect here. |
| `port` | TCP port (always 27017). |
| `cluster_arn` | Cluster ARN. |
| `cluster_resource_id` | DocumentDB cluster resource ID. |
| `master_secret_arn` | ARN of the master-credentials Secret. |
| `readonly_secret_arn` | ARN of the read-only-credentials Secret (the only one shared with consumers). |
| `kms_key_arn` | KMS CMK used for storage and Secrets encryption. |
| `security_group_id` | First SG attached to the cluster ENIs (convenience for SG-to-SG references). |
| `tls_ca_bundle_url` | URL to download `global-bundle.pem`. |
| `cluster_id` | Cluster identifier. |
| `instance_endpoints` | Per-instance endpoints. |
| `instance_identifiers` | Per-instance identifiers. |
| `master_secret_name` | Friendly name of the master secret. |
| `readonly_secret_name` | Friendly name of the read-only secret. |
| `parameter_group_name` | Cluster parameter group name. |
| `db_subnet_group_name` | DB subnet group name. |
| `master_username` | `docdb_admin`. |
| `readonly_username` | `biodata_reader`. |
| `db_name` | Logical database name carried on both secrets. |

`master_password` and `readonly_password` are intentionally NOT exported —
fetch them from Secrets Manager via the corresponding `*_secret_arn`.

---

## Example usage

In `terraform/envs/dev/main.tf`:

```hcl
module "vpc" {
  source = "../../modules/vpc"
  # ...
}

module "documentdb" {
  source = "../../modules/documentdb"

  name_prefix = "biodata-registry-dev"
  environment = "dev"
  project     = "biodata-registry"

  vpc_id             = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids
  security_group_ids = [module.vpc.internal_security_group_id]

  # PoC defaults — override for production
  instance_class = "db.r6g.large"
  instance_count = 1

  tags = {
    Owner = "biodata-registry-team"
  }
}
```

---

## Validation

This module is consumed by the dev environment composition and is not
deployed standalone. To verify the module compiles:

```bash
cd customers/NPO/RSC/Allen_Institute/biodata-registry/terraform/modules/documentdb
terraform init -backend=false
terraform validate
terraform fmt -check
```

`terraform plan` / `apply` is run in **Task 10** against the dev
environment composition.

---

## TODOs handed to downstream tasks

* **Task 8 (migration runner)** — replace the operator-workstation mongosh
  bootstrap with a one-shot Lambda inside the VPC so `terraform apply` no
  longer requires VPC reach from the operator's machine.
* **Task 12 (Lambda Layer)** — bake `global-bundle.pem` into the Lambda
  layer; export the file path as `DOCDB_TLS_CA_FILE`.
* **Task 17 (CDC pipeline)** — wire the EventBridge Pipes target's IAM
  role to grant `secretsmanager:GetSecretValue` on `master_secret_arn`
  only, and `kms:Decrypt` on `kms_key_arn`.
* **Task 18 (Indexing_Lambda)** — same IAM grants as above; the Indexing
  Lambda performs the actual writes onto DocumentDB collections.
* **Task 18.3** — write `docs/docdb-access.md` for `aind-data-access-api`
  consumers, lifting the connection example above and the trust-model
  caveat verbatim.
* **Production hardening** — narrow the read-only role from
  `readAnyDatabase` to `read` on `biodata_registry`, enable a per-query
  audit log review pipeline, and replace `local-exec` mongosh with a
  one-shot Lambda.

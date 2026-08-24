# dev environment composition

End-to-end Terraform composition for the Allen BioData Registry PoC dev
environment. One `terraform apply` here provisions the entire QC1 stack:
VPC + Aurora + DocumentDB + OpenSearch Serverless + ElastiCache + Cognito
+ Post-Confirmation Lambda + CloudFront + S3 web bucket, and runs the seven
SQL migrations against Aurora via the migration runner Lambda.

Validates **R32.5** (`terraform apply` runs clean and is idempotent) and
**R32.6** (remote state).

> **Account / region:** AWS account `014097726564`, region `us-west-2`.
> The composition refuses to apply against any other account (precondition
> on `data.aws_caller_identity.current`).

---

## What this composition provisions

| Module | What it creates | Cost (idle PoC) |
| --- | --- | --- |
| `vpc` | 3-AZ VPC, 3 private + 3 public subnets, single NAT, S3/DDB gateway endpoints, Bedrock/Cognito/Secrets/KMS interface endpoints | ~$70/mo (1 NAT + 5 interface endpoints) |
| `aurora` | Aurora PostgreSQL 16.13 Serverless v2 cluster, KMS CMK, master Secrets Manager secret, parameter group with `rds.logical_replication = 1` and `pgvector` preload | ~$43/mo (0.5 ACU floor) |
| `documentdb` | DocumentDB 5.0 cluster (1 × `db.r6g.large`), KMS CMK, master + read-only Secrets Manager secrets | ~$210/mo |
| `opensearch` | OpenSearch Serverless SEARCH-type collection, KMS CMK, synonyms S3 bucket, VPC endpoint | ~$350/mo (standby DISABLED) |
| `elasticache` | Redis 7.1 replication group (2 × `cache.t4g.micro`, automatic failover), KMS CMK, AUTH-token secret | ~$26/mo |
| `cognito` | User Pool + hosted UI domain, web client, optional SAML federation, Post-Confirmation trigger wired | $0 (≤50K MAU free tier) |
| `cloudfront-s3` | React app S3 bucket, KMS CMK, CloudFront distribution (PriceClass_100), security headers policy | <$5/mo at PoC traffic |
| `lambdas/post-confirmation` | Cognito Post-Confirmation Lambda (creates `app_user` row in Aurora) | $0 |
| `lambdas/migration-runner` | Migration runner Lambda (applies the 7 SQL migrations on every apply) | $0 |

Total idle floor: **~$700/mo**. Active use adds Lambda + API Gateway +
Bedrock token costs once Phases 2/3 land.

---

## Prerequisites

1. **Terraform `>= 1.5`** (validated against 1.15.2 in the bootstrap dir).
2. **AWS CLI** authenticated against account `014097726564`. Use ada:

   ```bash
   ada credentials update --account 014097726564 --role Admin --provider isengard
   ```

3. **Export those creds into the current shell** (the helper script below
   sets `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN`
   from the latest `ada` print). Required because Terraform's S3 backend
   reads creds from env vars, not the AWS CLI profile cache:

   ```bash
   source ../../bootstrap/.creds-helper.sh
   ```

   The helper script lives next to the bootstrap config (already created
   in Task 1). Re-run it any time `aws sts get-caller-identity` starts
   returning `ExpiredToken`.

4. **Python 3.12** on PATH. The composition runs `python3 -m pip install`
   to package the Post-Confirmation and migration-runner Lambdas. Override
   with `-var python_executable=python3.12` if `python3` is not 3.12.

5. **Remote state backend already provisioned.** The bootstrap config
   (`terraform/bootstrap/`) must have been applied once in this account.
   Confirm with:

   ```bash
   aws s3api head-bucket \
     --bucket biodata-registry-tf-state-014097726564-us-west-2

   aws dynamodb describe-table \
     --table-name biodata-registry-tf-locks \
     --region us-west-2
   ```

   Both should return cleanly.

---

## First-time init

```bash
cd customers/NPO/RSC/Allen_Institute/biodata-registry/terraform/envs/dev

terraform init
terraform validate
terraform fmt -check -recursive ../..
```

`terraform init` downloads the AWS, archive, null, and random providers and
configures the S3 backend. `terraform validate` should print
`Success! The configuration is valid.`. `fmt -check` should exit 0.

---

## First apply (clean account)

```bash
terraform plan -out=dev.tfplan
terraform apply dev.tfplan
```

**Expect 12–25 minutes** for the first apply, dominated by:

- Aurora cluster + first instance: ~7–10 minutes
- DocumentDB cluster + first instance: ~10–15 minutes (the long pole)
- OpenSearch Serverless collection: ~3–5 minutes
- Everything else: ≤2 minutes combined

The migration runner Lambda is invoked synchronously at the end of the
graph; its log output (with the list of applied migrations) is captured
in `terraform output migration_runner_invocation_result` once apply
completes:

```bash
terraform output -json migration_runner_invocation_result | jq .
```

Expected shape on a clean apply:

```json
{
  "applied": [
    "0001_governance.sql",
    "0002_data_asset.sql",
    "0003_junctions.sql",
    "0004_revisions_lifecycle_duplicates.sql",
    "0005_collections_schemas.sql",
    "0006_rls_policies.sql",
    "0007_search_indexes.sql"
  ],
  "skipped": [],
  "drift": [],
  "schema_version_created": true,
  "elapsed_ms": 1234
}
```

### Known issues to expect

| Symptom | Cause | Resolution |
| --- | --- | --- |
| `Error: validating provider credentials` at plan time | Stale ada creds | `ada credentials update ...` then re-source the helper |
| `caller account NNN does not match var.account_id` | Wrong AWS profile / wrong creds | Refresh creds against `014097726564` |
| Plan fails on `aws_vpc_endpoint.interface["...bedrock-agent-runtime"]` (PrivateLink not GA in target region) | Bedrock Agent Runtime PrivateLink not yet in this region | `terraform apply -var=enable_bedrock_agent_runtime_endpoint=false` |
| Cognito SAML metadata URL placeholder fails | `var.cognito_saml_metadata_url` still null and somebody set the SAML toggle on | Leave `cognito_saml_metadata_url = null` for the PoC; SAML is opt-in |
| ACM cert validation hangs (only when `enable_custom_domain = true`) | Customer hasn't created the validation CNAME records | Run `terraform output acm_validation_records` and add them to the DNS zone — apply waits up to 72h |
| Migration runner Lambda times out (>5 min) | Network ACL / SG rule blocking 5432 to Aurora | Confirm `module.vpc.internal_security_group_id` is on both sides; check Lambda CloudWatch logs |

---

## Idempotency check

This is the explicit success criterion for **Task 10**.

```bash
terraform plan
```

After the first successful apply, a fresh `terraform plan` MUST report:

```
No changes. Your infrastructure matches the configuration.
```

If the plan is non-empty, the diff is the bug. Common offenders:

- `module.aurora.aws_secretsmanager_secret_version.master` showing a diff
  on every plan: the master password regenerated. Ensure
  `random_password.master` has stable `keepers`.
- Lambda `source_code_hash` flipping: usually means `null_resource.package`
  is rebuilding because something in `services/{post-confirmation,migration-runner}`
  changed. If the source genuinely did not change, check for `__pycache__`
  contamination — the package step should strip it.
- Migration runner `invocation_result` showing a diff: the runner is
  seeing different output. If migrations are stable, check that
  `aws_lambda_invocation.migrate.triggers` are deterministic.

Re-run `terraform apply` to flatten any spurious diff before declaring
victory.

---

## Manual seeding (until Task 9.1 lands)

The seeder Terraform module is deferred (see `main.tf` "seeder — DEFERRED"
section). Run the seeder manually after `terraform apply`:

```bash
# From an SSM session or bastion inside the VPC:
cd ../../../seed
python seeder.py \
  --aurora-secret-arn "$(terraform -chdir=../terraform/envs/dev output -raw aurora_master_secret_arn)" \
  --source s3://aind-scratch-data/jon.young/metadata_v2_records_20260324/data_assets.json \
  --sample 0.10
```

The seeder is idempotent (content-hash dedupe), safe to re-run.

---

## Tear-down

```bash
terraform destroy
```

Tear-down takes 10–20 minutes (DocumentDB cluster delete is the long pole).
The PoC defaults skip final snapshots and disable deletion protection on
both Aurora and DocumentDB, so destroy works cleanly. Production overrides
must flip `skip_final_snapshot = false` and `deletion_protection = true`
on both data-plane modules — those are documented in their README files.

The S3 buckets (synonyms, web app, optional CloudFront logs) have
`force_destroy = true` only where the variable defaults allow; otherwise
empty them manually before re-running destroy:

```bash
aws s3 rm "s3://$(terraform output -raw webapp_bucket_name)" --recursive
```

The Terraform state bucket and lock table created by the bootstrap config
are NOT destroyed by this composition — `terraform destroy` here only
removes the resources in this root. To remove the backend itself, see
`terraform/bootstrap/README.md` "Tear-down (rare)".

---

## Useful diagnostic commands

```bash
# Full output dump after apply
terraform output

# Just the QC1-demo URLs
terraform output -raw cloudfront_distribution_domain
terraform output -raw cognito_hosted_ui_domain

# psql into Aurora (from inside the VPC):
SECRET_ARN=$(terraform output -raw aurora_master_secret_arn)
HOST=$(terraform output -raw aurora_cluster_endpoint)
PASS=$(aws secretsmanager get-secret-value --secret-id "$SECRET_ARN" \
       --query SecretString --output text | jq -r .password)
PGPASSWORD="$PASS" psql --host="$HOST" --username=biodata_admin \
  --dbname=biodata_registry --set=ON_ERROR_STOP=1

# Re-invoke the migration runner manually (idempotent):
aws lambda invoke \
  --function-name "$(terraform output -raw migration_runner_function_name)" \
  --payload '{}' \
  /tmp/migrate.out
cat /tmp/migrate.out | jq .
```

---

## Module wiring graph

```
bootstrap (one-time, separate state)
   │  S3 + DynamoDB + KMS for remote state
   ▼
vpc ────────────────────────────────────────────────────────────────┐
 │  vpc_id, private_subnet_ids, internal_security_group_id          │
 │                                                                  │
 ├──► aurora ─────────────► migration_runner ──► (seeder, deferred) │
 │     │                       (invokes 7 SQL                       │
 │     │                        migrations on apply)                │
 │     │                                                            │
 │     └──► post_confirmation_lambda ──► cognito                    │
 │                                                                  │
 ├──► documentdb (CDC sink)                                         │
 ├──► opensearch (search + KNN)                                     │
 └──► elasticache (4 cache tiers, single replication group)         │
                                                                    │
cloudfront_s3 (provider alias us_east_1 for ACM) ◄──────────────────┘
```

---

## File map

| File | Purpose |
| --- | --- |
| `versions.tf` | Pinned Terraform + provider versions |
| `variables.tf` | All environment inputs (region, account, sizing, toggles) |
| `main.tf` | Backend declaration, providers, module wiring |
| `outputs.tf` | Endpoints + IDs surfaced to the operator |
| `README.md` | This runbook |

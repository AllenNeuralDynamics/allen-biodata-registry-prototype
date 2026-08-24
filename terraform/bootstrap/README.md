# Terraform Bootstrap — Remote State Backend

One-time bootstrap that provisions the Terraform **remote state backend** for the Allen BioData Registry PoC. Every other Terraform module in this repo (`vpc`, `aurora`, `documentdb`, `opensearch`, `cdc-pipeline`, etc.) stores its state in the resources created here.

> **Run this exactly once per AWS account / region pair.** After it succeeds, downstream modules wire their `backend "s3"` blocks to the bucket and lock table this config emits.

Validates **R32.6** — *"THE Terraform stack SHALL use remote state storage (S3 + DynamoDB lock table) to support team collaboration and safe concurrent operations."*

Design reference: `design.md` § Infrastructure as Code → Remote State Backend.

---

## What gets created

| Resource | Name | Purpose |
| --- | --- | --- |
| KMS CMK + alias | `alias/biodata-registry-tf-state` | SSE for the state bucket; rotation enabled |
| S3 bucket | `biodata-registry-tf-state-{account_id}-{region}` | Terraform state objects (versioned, KMS-encrypted, public-blocked, 90-day NCV → Glacier) |
| DynamoDB table | `biodata-registry-tf-locks` | State locking. `PAY_PER_REQUEST`, `hash_key = "LockID"`, SSE on, PITR on |
| IAM managed policy | `biodata-registry-tf-backend-access` | Scoped (non-wildcard) backend access; attach to operator and CI roles |

The IAM policy grants only:

- `s3:ListBucket` on the state bucket
- `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` on objects under the state bucket
- `dynamodb:PutItem`, `dynamodb:GetItem`, `dynamodb:DeleteItem` on the lock table
- `kms:Encrypt`, `kms:Decrypt`, `kms:GenerateDataKey`, `kms:DescribeKey` on the CMK

No wildcards, no `s3:*`, no `dynamodb:*`, no `kms:*`.

---

## Why this config uses LOCAL state

The bootstrap creates the very resources Terraform would otherwise use to store its state. Pointing this config at a remote S3 backend that does not yet exist is impossible — chicken and egg. So this config keeps state on local disk (`terraform.tfstate`), and **after apply you save that state file somewhere safe** (private repo, password vault, encrypted ops bucket).

Once the bootstrap is applied:

- Re-runs of the bootstrap are rare (only to rotate the CMK or modify the IAM policy).
- The main stack and per-module stacks all use the S3 backend defined here.

---

## Prerequisites

- Terraform `>= 1.5` (this repo is validated against 1.15.2)
- AWS CLI authenticated against the target account with permissions to create KMS keys, S3 buckets, DynamoDB tables, and IAM policies (admin or equivalent)
- The AWS region you want the backend in. Default: `us-west-2`

Confirm your identity before running:

```bash
aws sts get-caller-identity
```

---

## Run the bootstrap

```bash
cd customers/NPO/RSC/Allen_Institute/biodata-registry/terraform/bootstrap

terraform init
terraform validate
terraform plan -out=bootstrap.tfplan
terraform apply bootstrap.tfplan
```

Confirm idempotency with a second apply — should report `No changes`:

```bash
terraform apply -auto-approve
```

Optional region override:

```bash
terraform apply -var="aws_region=us-east-1"
```

---

## Verify the backend exists

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=us-west-2

aws s3api head-bucket \
  --bucket "biodata-registry-tf-state-${ACCOUNT_ID}-${REGION}"

aws dynamodb describe-table \
  --table-name biodata-registry-tf-locks \
  --region "$REGION" \
  --query 'Table.{Status:TableStatus,Hash:KeySchema[0].AttributeName,Billing:BillingModeSummary.BillingMode}'

aws kms describe-key \
  --key-id alias/biodata-registry-tf-state \
  --region "$REGION" \
  --query 'KeyMetadata.{Arn:Arn,Enabled:Enabled,Rotation:KeyRotationEnabled}'
```

---

## After apply: protect the bootstrap state file

The local `terraform.tfstate` file contains the KMS key id, bucket name, table arn, and IAM policy arn. **Do not commit it to source control.** A `.gitignore` in this directory excludes `*.tfstate*` already, but you must:

1. Copy `terraform.tfstate` to secure ops storage (private repo, encrypted vault, or KMS-encrypted internal S3 bucket — *not* this newly-created bucket, since that would re-introduce the chicken-and-egg problem).
2. Document the storage location for your team.
3. To run subsequent bootstraps (e.g., to rotate the CMK), restore the state file first.

---

## Wiring the main stack to this backend

Every downstream Terraform module's `backend.tf` should look like:

```hcl
terraform {
  backend "s3" {
    bucket         = "biodata-registry-tf-state-014097726564-us-west-2"
    key            = "<module-name>/terraform.tfstate"   # e.g. "vpc/terraform.tfstate"
    region         = "us-west-2"
    dynamodb_table = "biodata-registry-tf-locks"
    encrypt        = true
    kms_key_id     = "alias/biodata-registry-tf-state"
  }
}
```

Operator and CI principals must have the `biodata-registry-tf-backend-access` policy attached. Get its ARN from this stack's outputs:

```bash
terraform output backend_access_policy_arn
```

Attach with `aws iam attach-role-policy` (or via your role-management Terraform) and downstream `terraform init` / `apply` will succeed without granting overly broad permissions.

---

## Outputs

| Output | Description |
| --- | --- |
| `state_bucket_name` | S3 bucket for downstream `backend "s3" { bucket = ... }` |
| `state_bucket_arn` | ARN of the state bucket |
| `lock_table_name` | DynamoDB lock table for downstream `backend "s3" { dynamodb_table = ... }` |
| `lock_table_arn` | ARN of the lock table |
| `kms_key_arn` | CMK ARN — for the IAM policy and (optionally) `backend "s3" { kms_key_id = ... }` |
| `kms_key_alias` | CMK alias |
| `backend_access_policy_arn` | Scoped IAM policy for operator / CI roles |
| `aws_region` / `aws_account_id` | Confirmation of the target account/region |

---

## Tear-down (rare)

The S3 bucket and DynamoDB table both have `lifecycle { prevent_destroy = true }`. To intentionally remove the backend:

1. Confirm every downstream module has been destroyed and no longer references this backend.
2. Empty the state bucket (including all noncurrent versions).
3. Remove the `prevent_destroy` lines, run `terraform apply`, then `terraform destroy`.

This is intentionally inconvenient — accidentally destroying the state bucket would orphan every other Terraform stack in this account.

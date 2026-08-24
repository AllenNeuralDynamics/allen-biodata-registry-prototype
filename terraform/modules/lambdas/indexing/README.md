# `lambdas/indexing` Terraform module

Provisions the Indexing Lambda (Task 18.1) — the CDC consumer that
fans Aurora WAL events out to DocumentDB and OpenSearch. Wires it as
the consumer of the SQS FIFO queue produced by the
[`cdc-pipeline`](../../cdc-pipeline/) module.

**Validates:** R1.7, R8.4, R17.9, R28.3, R28.4, R28.5, R28.6.

**Design references:**
- `design.md` §Components.12. Indexing_Lambda.
- `design.md` §Architecture.CDC Pipeline Architecture.
- `design.md` §IaC.Terraform Modules (`lambdas/indexing`).

## What this module provisions

- An `aws_lambda_function` named `${name_prefix}-indexing`, runtime
  Python 3.12, handler `handler.lambda_handler`, with the source
  packaged from `services/indexing-lambda/`. `pip install` against
  `requirements.txt` bundles psycopg, pymongo, opensearch-py, and
  requests-aws4auth into the deployment zip.
- An `aws_lambda_event_source_mapping` from the cdc-pipeline's main
  FIFO queue to the Lambda. Batch size 10, batch window 5s.
- An IAM execution role with the minimum policies:
  - `AWSLambdaVPCAccessExecutionRole` (managed) for ENI mgmt + Logs.
  - `secretsmanager:GetSecretValue` on Aurora + DocumentDB secrets.
  - `aoss:APIAccessAll` on the OpenSearch Serverless collection ARN.
  - SQS Receive/Delete/GetQueueAttributes on the source queue.
  - `sqs:SendMessage` on the DLQ.
  - `kms:Decrypt` on Aurora + DocumentDB CMKs (when supplied).
- A CloudWatch Logs group with 14-day retention.
- Two CloudWatch alarms:
  - Invocation errors > 5 over 5 minutes.
  - DLQ depth > 0 (any message in DLQ pages immediately).

## What this module does NOT provision

- The shared Lambda Layer (`biodata_registry_shared`). The Indexing
  Lambda doesn't depend on the Layer for its core path; pass
  `shared_layer_arn = null` (the default) unless a future revision
  adds a Layer-only helper.
- The DLQ itself. The DLQ is shared with the cdc-pipeline module —
  pass its `dlq_url` and `dlq_arn` outputs as inputs here.

## Usage

```hcl
module "indexing_lambda" {
  source = "../../modules/lambdas/indexing"

  name_prefix = "biodata-registry-dev"
  environment = "dev"
  project     = "biodata-registry"
  region      = "us-west-2"

  source_dir        = "${path.root}/../../services/indexing-lambda"
  python_executable = "python3"

  # Aurora — privileged BYPASSRLS connection.
  aurora_secret_arn  = aws_secretsmanager_secret.cdc_indexer.arn
  aurora_host        = module.aurora.cluster_endpoint
  aurora_port        = module.aurora.port
  aurora_db_name     = module.aurora.db_name
  aurora_kms_key_arn = module.aurora.kms_key_arn

  # DocumentDB — service-to-service inside VPC.
  docdb_secret_arn  = module.documentdb.master_secret_arn
  docdb_endpoint    = module.documentdb.cluster_endpoint
  docdb_kms_key_arn = module.documentdb.kms_key_arn

  # OpenSearch.
  opensearch_endpoint       = module.opensearch.collection_endpoint
  opensearch_collection_arn = module.opensearch.collection_arn

  # CDC pipeline source queue + DLQ.
  source_sqs_queue_arn = module.cdc_pipeline.main_queue_arn
  source_sqs_queue_url = module.cdc_pipeline.main_queue_url
  dlq_arn              = module.cdc_pipeline.dlq_arn
  dlq_url              = module.cdc_pipeline.dlq_url

  # Networking.
  subnet_ids         = module.vpc.private_subnet_ids
  security_group_ids = [module.vpc.internal_security_group_id]

  reserved_concurrency = 10
}

# Wire the Lambda's IAM role into the cdc-pipeline's queue policy so
# the queue itself permits this consumer.
module "cdc_pipeline" {
  source = "../../modules/cdc-pipeline"
  # ...
  consumer_lambda_role_arns = [module.indexing_lambda.iam_role_arn]
}
```

## Trust boundary

The Indexing Lambda runs as a privileged service identity. It connects
to Aurora as a Postgres role with `BYPASSRLS` so it can see every row
regardless of governance scope — this is the deliberate design choice
(see the handler module docstring and the Lambda's README). The
`space_id`, `org_id`, and `is_sensitive` fields it writes to the read
stores are how downstream consumers enforce access control:

- **OpenSearch**: `Search_Lambda` adds `is_sensitive: false` and
  `space_id IN [...]` filter clauses for non-privileged users.
- **DocumentDB**: the `aind-data-access-api` client library applies
  the equivalent filters in its query layer (DocumentDB itself is a
  VPC-internal trust boundary, not an RLS-enforced boundary — see
  `design.md` §Design Decisions.DocumentDB Access Model).

## Build platform sensitivity

The Lambda zip bundles binary wheels for psycopg, pymongo, and
requests-aws4auth's dependencies. These wheels are platform-specific
(Linux x86_64 manylinux). Operators on macOS or Windows must build
inside a Linux container to produce a working zip:

```bash
# In production CI, set var.python_executable to:
docker run --rm -v "$PWD":/var/task public.ecr.aws/lambda/python:3.12 pip
```

The PoC default (`python3`) works for `terraform validate` /
`terraform plan` on any platform; only `terraform apply` from a non-
Linux host produces a broken zip.

## Variables — quick reference

See `variables.tf` for full descriptions and validation rules.

| Variable | Purpose |
|----------|---------|
| `source_dir` | Absolute path to `services/indexing-lambda/`. |
| `aurora_secret_arn` | Secret with `cdc_indexer` Aurora credentials. |
| `aurora_host` | Aurora writer endpoint. |
| `docdb_secret_arn` | Secret with DocumentDB master credentials. |
| `docdb_endpoint` | DocumentDB cluster endpoint hostname. |
| `opensearch_endpoint` | OpenSearch Serverless collection URL. |
| `opensearch_collection_arn` | For `aoss:APIAccessAll` policy scoping. |
| `source_sqs_queue_arn` | From `cdc-pipeline.main_queue_arn`. |
| `dlq_url` / `dlq_arn` | From `cdc-pipeline.dlq_url` / `dlq_arn`. |
| `subnet_ids` | Private subnets routing to Aurora/DocDB/OpenSearch. |
| `security_group_ids` | SGs permitting egress to the data plane. |
| `reserved_concurrency` | Default 10 — caps concurrency vs Aurora pool. |

## Outputs

| Output | Use |
|--------|-----|
| `lambda_arn` | Wire into observability dashboards. |
| `iam_role_arn` | Pass to `cdc-pipeline.consumer_lambda_role_arns`. |
| `log_group_name` | Tail with `aws logs tail`. |
| `error_alarm_arn` | Compose into upstream alarm trees. |
| `dlq_alarm_arn` | Compose into DLQ runbook. |
| `event_source_mapping_uuid` | For runbook ops on the mapping. |

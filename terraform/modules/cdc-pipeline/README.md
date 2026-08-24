# `cdc-pipeline` — Aurora WAL → SQS FIFO buffer for the Indexing_Lambda

Allen BioData Registry PoC, Phase 2 (QC2). Provisions the CDC transport
between Aurora's `biodata_cdc` logical replication slot and the future
Indexing_Lambda (Task 18.1). Validates **R28.1, R28.2, R28.6**.

Design references:
- `design.md` §Architecture.CDC Pipeline Architecture
- `design.md` §IaC.Terraform Modules (`cdc-pipeline`)
- `design.md` §Design Decisions.Why MSK or EventBridge Pipes for the CDC transport?
- `design.md` §Error Handling.Failure Domains (CDC transport failure)

---

## What this module provisions

```
┌──────────────────┐    ┌────────────────────┐    ┌──────────────────┐
│ Aurora           │    │ CDC Reader Lambda  │    │ SQS FIFO         │
│ biodata_cdc slot │──▶ │ (1-min schedule)   │──▶ │ <prefix>-cdc-    │
│ pgoutput plugin  │    │ rds-db:connect IAM │    │ main.fifo        │
└──────────────────┘    └────────────────────┘    └────────┬─────────┘
                                                            │ N failed receives
                                                            ▼
                                                   ┌──────────────────┐
                                                   │ SQS FIFO DLQ     │
                                                   │ <prefix>-cdc-    │
                                                   │ dlq.fifo         │ ──▶ CloudWatch alarm
                                                   └──────────────────┘
                                                            ▲
                                                            │ ReceiveMessage
                                                   ┌────────┴──────────┐
                                                   │ Indexing_Lambda    │
                                                   │ (Task 18.1)        │
                                                   └────────────────────┘
```

| Resource | Purpose |
|---|---|
| `aws_sqs_queue.cdc_main` | FIFO queue, content-based dedup, deduplication scope = `messageGroup`, redrive → DLQ after `var.max_receive_count` failed receives, KMS-encrypted, 5-min visibility, 4-day retention. |
| `aws_sqs_queue.cdc_dlq` | FIFO DLQ, 14-day retention, KMS-encrypted. |
| `aws_sqs_queue_policy.cdc_main_consumer` (count) | Allows the Indexing_Lambda's exec role(s) to Receive / Delete / GetQueueAttributes on the main queue. Created only when `var.consumer_lambda_role_arns` is non-empty. |
| `aws_sqs_queue_policy.cdc_dlq_consumer` (count) | Same for the DLQ replay path. |
| `aws_iam_role.cdc_reader_exec` + scoped policies | IAM execution role for the CDC Reader Lambda. Grants VPC ENI mgmt + CloudWatch Logs, `rds-db:connect` to one DB user, `sqs:SendMessage` on the main queue, and KMS data-key access when a CMK is supplied. |
| `aws_lambda_function.cdc_reader` | Python 3.12 Lambda packaged from `var.cdc_reader_source_dir` (or the in-tree placeholder when null). |
| `aws_iam_role.scheduler` + scoped policy | Service role assumed by EventBridge Scheduler to invoke the CDC Reader. |
| `aws_scheduler_schedule.cdc_reader` | Fires the CDC Reader on `var.cdc_reader_schedule_expression` (default `rate(1 minute)`). |
| `aws_cloudwatch_metric_alarm.dlq_not_empty` | Fires when `ApproximateNumberOfMessagesVisible > 0` on the DLQ for `var.dlq_alarm_evaluation_periods` × 60s. |
| `aws_cloudwatch_log_group.cdc_reader` | Per-Lambda log group with retention pinned via variable. |

---

## ⚠️ Implementation Gap — read before relying on this module

**AWS EventBridge Pipes does NOT natively support PostgreSQL logical
replication slots as a source.** The pipe sources actually supported are
DDB Streams, Kinesis, MSK, Self-Managed Apache Kafka, SQS, RabbitMQ /
Amazon MQ. A "Pipe from Aurora" therefore needs an upstream relay —
either MSK + Debezium (the canonical Kafka-based path) or AWS DMS with
a Kinesis target.

For the PoC, this module picks the simplest viable option that makes
the Indexing_Lambda's interface stable: a "CDC Reader" Lambda invoked
on a one-minute EventBridge schedule that:

1. Mints a short-lived IAM token via `boto3.client('rds').generate_db_auth_token`.
2. Opens a replication connection to Aurora's writer endpoint.
3. Attaches to the `biodata_cdc` slot via the `pgoutput` plugin and the
   `biodata_cdc_pub` publication.
4. Reads up to a batch's worth of WAL messages.
5. For each event, sends a FIFO SQS message with
   `MessageGroupId = sha256(table || ':' || pk)` and
   `MessageDeduplicationId = lsn`.
6. Advances the slot's `confirmed_flush_lsn` so consumed events are
   not redelivered.

**What ships in Task 17.1**: the SQS queues, the Indexing_Lambda IAM
allow-list scaffolding, the CDC Reader Lambda's IAM execution role +
schedule + log group + DLQ alarm, and a tiny **placeholder** CDC Reader
handler that logs a warning and exits. The placeholder's only job is
to make `terraform validate` and `terraform apply` succeed end-to-end
before the production handler exists.

**What does NOT work end-to-end after applying this module alone**:
the slot is not actually drained. CDC events accumulate in Aurora's
WAL until either (a) the production CDC Reader handler lands under
`services/cdc-reader/` (Task 18.x) and `var.cdc_reader_source_dir` is
pointed at it, or (b) the MSK upgrade path is taken via
`var.enable_msk_upgrade_path` (reserved — not implemented in 17.1).

The Indexing_Lambda (Task 18.1) consumes the SAME SQS contract
regardless of which approach drains the slot. The swap is therefore
transparent at the consumer.

### Trade-offs of the chosen approach (Lambda on a schedule)

| Aspect | Lambda-on-schedule (this module) | MSK + Debezium (upgrade path) |
|---|---|---|
| Cost (PoC volume) | ~$0.04/mo (1 invocation/min) | ~$200/mo (MSK Serverless floor) |
| End-to-end latency | 30–90s (one invocation cycle + processing) | <2s |
| Throughput ceiling | ~1k events/min before the slot backs up | 100k+ events/sec |
| Operational surface | One Lambda + one queue | Kafka cluster + connector |
| Ordering guarantees | Per-row (via `MessageGroupId`) | Per-row (via Kafka partition key) |
| Recovery model | Slot retains LSN; next invocation resumes | Connector commits offsets |
| Suitability for PoC | ✅ ideal | ❌ overkill |

The PoC traffic envelope (10s of concurrent users, single-digit writes
per second sustained) is well inside the Lambda-on-schedule window;
the design.md latency budget of "≤5s end-to-end at PoC scale" is
**not** met by this approach (a 1-minute schedule means worst-case
visibility is ~60s + DocDB/OS write time). For the **QC2 demo** (Task
20) we either tighten the schedule to `rate(30 seconds)` or accept the
relaxed budget — the customer is aware of the gap.

---

## Inputs

### Required

| Variable | Source |
|---|---|
| `aurora_cluster_endpoint` | `module.aurora.cluster_endpoint` |
| `aurora_cluster_resource_id` | `module.aurora.cluster_resource_id` |
| `vpc_subnet_ids` | `module.vpc.private_subnet_ids` |
| `vpc_security_group_ids` | `[module.vpc.internal_security_group_id]` |

### Optional (defaults shown)

| Variable | Default | Notes |
|---|---|---|
| `aurora_db_user_for_cdc` | `"cdc_reader"` | Must exist + have `rds_iam` membership + `REPLICATION` attribute. Created by the migration runner. |
| `db_port` | `5432` | |
| `db_name` | `"biodata_registry"` | |
| `cdc_replication_slot_name` | `"biodata_cdc"` | |
| `cdc_publication_name` | `"biodata_cdc_pub"` | |
| `sqs_kms_key_arn` | `null` | When null, the SQS-managed `alias/aws/sqs` key is used. |
| `max_receive_count` | `3` | Failed receives before redrive to DLQ. |
| `main_visibility_timeout_seconds` | `300` (5 min) | |
| `main_message_retention_seconds` | `345600` (4 days) | |
| `dlq_message_retention_seconds` | `1209600` (14 days) | |
| `cdc_reader_source_dir` | `null` (placeholder) | Set to `"${path.root}/../../../services/cdc-reader"` once Task 18.x lands. |
| `cdc_reader_memory_mb` | `512` | |
| `cdc_reader_timeout_seconds` | `50` | < 60s schedule cadence so invocations don't pile up. |
| `cdc_reader_schedule_expression` | `"rate(1 minute)"` | |
| `consumer_lambda_role_arns` | `[]` | Pass `[module.indexing_lambda.role_arn]` once Task 18.1 exists. |
| `dlq_alarm_actions` | `[]` | Pass an SNS topic ARN to actually page on-call. |
| `enable_msk_upgrade_path` | `false` | Reserved — currently rejected via validation. |

---

## Outputs

| Output | What it is |
|---|---|
| `main_queue_arn` / `main_queue_url` / `main_queue_name` | The FIFO queue the Indexing_Lambda consumes. |
| `dlq_arn` / `dlq_url` / `dlq_name` | The DLQ. |
| `cdc_reader_function_arn` / `cdc_reader_function_name` | The CDC Reader Lambda. |
| `cdc_reader_role_arn` / `cdc_reader_role_name` | The CDC Reader's exec role. |
| `cdc_reader_log_group_name` / `cdc_reader_log_group_arn` | CloudWatch Logs group. |
| `scheduler_name` / `scheduler_arn` | EventBridge Scheduler schedule. |
| `dlq_alarm_arn` / `dlq_alarm_name` | DLQ depth alarm. |
| `cdc_replication_slot_name` / `cdc_publication_name` | Pass-through of the input vars. |
| `msk_upgrade_path_enabled` | Always false in 17.1; reserved. |

---

## Wiring example (dev composition)

```hcl
module "cdc_pipeline" {
  source = "../../modules/cdc-pipeline"

  name_prefix = local.name_prefix
  environment = var.environment
  project     = var.project

  aurora_cluster_endpoint    = module.aurora.cluster_endpoint
  aurora_cluster_resource_id = module.aurora.cluster_resource_id
  aurora_db_user_for_cdc     = "cdc_reader"
  db_port                    = module.aurora.port
  db_name                    = module.aurora.db_name
  cdc_replication_slot_name  = module.aurora.replication_slot_name

  vpc_subnet_ids         = module.vpc.private_subnet_ids
  vpc_security_group_ids = [module.vpc.internal_security_group_id]

  # Wire the Indexing_Lambda's exec role here once Task 18.1 lands.
  consumer_lambda_role_arns = []

  # Once services/cdc-reader/ ships, point at it:
  # cdc_reader_source_dir = "${path.root}/../../../services/cdc-reader"

  tags = var.tags
}
```

---

## Operational runbook (placeholder)

### Diagnose: why is the Indexing_Lambda not seeing events?

1. Check the CDC Reader's CloudWatch log group:
   `aws logs tail /aws/lambda/<prefix>-cdc-reader --follow`
2. If the placeholder warning appears (`cdc-reader placeholder invoked`),
   the production handler hasn't shipped yet — set
   `var.cdc_reader_source_dir` and re-apply.
3. If the production handler is running, check Aurora's slot lag:
   `SELECT slot_name, confirmed_flush_lsn, restart_lsn, pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn) AS lag_bytes FROM pg_replication_slots WHERE slot_name = 'biodata_cdc';`
4. Check the main queue depth:
   `aws sqs get-queue-attributes --queue-url <main_queue_url> --attribute-names ApproximateNumberOfMessages`
5. Check the DLQ depth and tail one message to read the failure metadata:
   `aws sqs receive-message --queue-url <dlq_url> --max-number-of-messages 1`

### Replay messages from the DLQ

The DLQ is FIFO-shaped to preserve message-group ordering. Replay
tooling (TBD, separate task) re-sends DLQ messages onto the main queue
in the original order. Until the tooling exists, manual replay is:

```bash
aws sqs receive-message --queue-url <dlq_url> --max-number-of-messages 10 \
  --wait-time-seconds 1 \
  | jq '.Messages[] | {Body, MessageGroupId: .Attributes.MessageGroupId}' \
  | jq -c '.' \
  | while read -r msg; do
      body=$(echo "$msg" | jq -r '.Body')
      group=$(echo "$msg" | jq -r '.MessageGroupId')
      aws sqs send-message --queue-url <main_queue_url> \
        --message-body "$body" \
        --message-group-id "$group"
    done
```

(Be careful: the example above does not delete the message from the
DLQ on success — production tooling must.)

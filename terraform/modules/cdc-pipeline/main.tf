###############################################################################
# Allen BioData Registry PoC — cdc-pipeline module
#
# Provisions the CDC transport between Aurora's `biodata_cdc` logical
# replication slot and the future Indexing_Lambda (Task 18.1):
#
#   Aurora WAL slot ──[CDC Reader Lambda, EventBridge schedule]──> SQS FIFO ──> Indexing_Lambda
#                                                                        \
#                                                                         └──> SQS DLQ (after N failed receives)
#
# Concretely:
#   * `aws_sqs_queue.cdc_main` — FIFO queue (.fifo suffix), content-based
#     deduplication, deduplication scope `messageGroup`, 5-minute
#     visibility timeout, 4-day retention, redrive → DLQ after N failed
#     receives. KMS-encrypted (CMK or AWS-managed). (R28.6.)
#   * `aws_sqs_queue.cdc_dlq` — Dead-letter queue, 14-day retention,
#     KMS-encrypted, FIFO to match the main queue's contract. (R28.6.)
#   * `aws_sqs_queue_policy.cdc_main_consumer` — allow-list of consumer
#     role ARNs (Indexing_Lambda's execution role) granted Receive /
#     Delete / GetQueueAttributes / ChangeMessageVisibility.
#   * `aws_sqs_queue_policy.cdc_dlq_consumer` — same but for the DLQ
#     replay path.
#   * `aws_iam_role.cdc_reader_exec` + scoped policies — execution role
#     for the CDC Reader Lambda. Grants:
#       - VPC ENI mgmt + CloudWatch Logs (managed policy).
#       - `rds-db:connect` scoped to {cluster_resource_id, db_user}.
#       - `sqs:SendMessage` + KMS GenerateDataKey on the main queue.
#   * `aws_lambda_function.cdc_reader` — placeholder Python 3.12 Lambda.
#     See "Implementation Gap" in README.md for what the production
#     handler must do.
#   * `aws_scheduler_schedule.cdc_reader` — EventBridge Scheduler that
#     fires the Lambda every minute (default) so the slot is drained
#     continuously.
#   * `aws_cloudwatch_metric_alarm.dlq_not_empty` — alarms on the DLQ's
#     `ApproximateNumberOfMessagesVisible > 0`. (R28.6 visibility.)
#
# Validates: R28.1, R28.2, R28.6.
# Design reference: design.md §Architecture.CDC Pipeline Architecture,
# §IaC.Terraform Modules (`cdc-pipeline`).
#
# IMPLEMENTATION GAP (called out explicitly):
#   AWS EventBridge Pipes does NOT natively support PostgreSQL logical
#   replication slots as a source. The choices documented in the design
#   are MSK + Debezium (proper streaming connector) or "EventBridge
#   Pipes" (which would require a relay component upstream — an AWS DMS
#   task with a Kinesis target, or a custom slot-draining Lambda). For
#   the PoC, this module picks the simplest viable option: a small
#   "CDC Reader" Lambda invoked on a 1-minute EventBridge schedule that
#   connects to Aurora, reads up to a batch from the slot via the
#   `pgoutput` plugin, sends each event to SQS as a FIFO message, then
#   advances the slot's confirmed_flush_lsn. The actual handler code
#   (services/cdc-reader/) is out of scope for Task 17.1 — see README.
#   The placeholder source tree this module ships is just enough to
#   make `terraform apply` succeed end-to-end.
###############################################################################

locals {
  common_tags = merge(
    {
      Project     = var.project
      Environment = var.environment
      Module      = "cdc-pipeline"
      ManagedBy   = "terraform"
    },
    var.tags,
  )

  # Names — kept tidy via locals so naming changes are one-line edits.
  queue_main_name           = "${var.name_prefix}-cdc-main.fifo" # FIFO queues MUST end in `.fifo`.
  queue_dlq_name            = "${var.name_prefix}-cdc-dlq.fifo"
  cdc_reader_function_name  = "${var.name_prefix}-cdc-reader"
  cdc_reader_log_group_name = "/aws/lambda/${var.name_prefix}-cdc-reader"
  scheduler_name            = "${var.name_prefix}-cdc-reader-schedule"
  dlq_alarm_name            = "${var.name_prefix}-cdc-dlq-not-empty"

  # If the caller did not provide a build_dir, stash the staging tree
  # under the calling Terraform working directory's `.terraform` cache.
  build_dir   = coalesce(var.build_dir, "${path.root}/.terraform/biodata-registry/${var.name_prefix}-cdc-reader-build")
  package_dir = "${local.build_dir}/package"
  zip_path    = "${local.build_dir}/cdc-reader.zip"

  # The placeholder source dir lives inside the module so a fresh
  # checkout can `terraform validate` without external dependencies.
  # When `var.cdc_reader_source_dir != null`, that directory wins.
  effective_source_dir = coalesce(var.cdc_reader_source_dir, "${path.module}/cdc-reader-placeholder")

  # Hash of the chosen source tree — drives package rebuilds.
  source_py_files = fileset(local.effective_source_dir, "**/*.py")
  source_hash = sha256(join("|", concat(
    [
      for f in local.source_py_files :
      filesha256("${local.effective_source_dir}/${f}")
      if !startswith(f, "tests/")
    ],
    [
      # Optional requirements.txt — fileset returns [] if absent, so the
      # one() helper short-circuits cleanly.
      for f in fileset(local.effective_source_dir, "requirements.txt") :
      filesha256("${local.effective_source_dir}/${f}")
    ],
  )))
}

data "aws_partition" "current" {}
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

###############################################################################
# SQS — DLQ first (the main queue's redrive_policy references it).
###############################################################################

resource "aws_sqs_queue" "cdc_dlq" {
  name = local.queue_dlq_name

  # FIFO so the DLQ preserves message-group ordering when operators
  # replay events back into the main queue.
  fifo_queue                  = true
  content_based_deduplication = true
  deduplication_scope         = "messageGroup"
  fifo_throughput_limit       = "perMessageGroupId"

  message_retention_seconds = var.dlq_message_retention_seconds

  # KMS encryption — CMK if provided, otherwise the SQS-managed key.
  # `kms_master_key_id` accepts an alias, key id, or ARN; passing the
  # alias when no CMK is supplied is the SQS convention.
  kms_master_key_id                 = var.sqs_kms_key_arn != null ? var.sqs_kms_key_arn : "alias/aws/sqs"
  kms_data_key_reuse_period_seconds = 300

  tags = merge(local.common_tags, {
    Name = local.queue_dlq_name
  })
}

###############################################################################
# SQS — main FIFO queue, redrives to the DLQ above.
###############################################################################

resource "aws_sqs_queue" "cdc_main" {
  name = local.queue_main_name

  fifo_queue = true

  # Content-based deduplication so the CDC Reader does not need to
  # compute its own dedup token — SQS hashes the message body. The
  # CDC Reader's MessageGroupId is the Aurora primary key (table_name +
  # PK), which preserves per-row ordering across retries.
  content_based_deduplication = true

  # `deduplication_scope = "messageGroup"` lets messages in DIFFERENT
  # groups deduplicate independently — exactly what we want for a CDC
  # stream where ordering only matters per row, not globally.
  deduplication_scope = "messageGroup"

  # Per-message-group throughput is the higher-throughput FIFO mode and
  # the right pairing with `messageGroup`-scoped deduplication.
  fifo_throughput_limit = "perMessageGroupId"

  visibility_timeout_seconds = var.main_visibility_timeout_seconds
  message_retention_seconds  = var.main_message_retention_seconds

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.cdc_dlq.arn
    maxReceiveCount     = var.max_receive_count
  })

  kms_master_key_id                 = var.sqs_kms_key_arn != null ? var.sqs_kms_key_arn : "alias/aws/sqs"
  kms_data_key_reuse_period_seconds = 300

  tags = merge(local.common_tags, {
    Name = local.queue_main_name
  })

  depends_on = [aws_sqs_queue.cdc_dlq]
}

###############################################################################
# SQS queue policies — allow the Indexing_Lambda's execution role(s) to
# consume from the queues. When `consumer_lambda_role_arns` is empty
# (the case while Task 17.1 lands before Task 18 is wired), the
# `aws_sqs_queue_policy` resources are simply not created — the queue's
# default IAM-only access stays in effect, which is correct.
###############################################################################

resource "aws_sqs_queue_policy" "cdc_main_consumer" {
  count = length(var.consumer_lambda_role_arns) > 0 ? 1 : 0

  queue_url = aws_sqs_queue.cdc_main.url

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowConsumerLambdas"
        Effect = "Allow"
        Principal = {
          AWS = var.consumer_lambda_role_arns
        }
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
          "sqs:GetQueueUrl",
          "sqs:ChangeMessageVisibility",
        ]
        Resource = aws_sqs_queue.cdc_main.arn
      },
    ]
  })
}

resource "aws_sqs_queue_policy" "cdc_dlq_consumer" {
  count = length(var.consumer_lambda_role_arns) > 0 ? 1 : 0

  queue_url = aws_sqs_queue.cdc_dlq.url

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowConsumerLambdasReplay"
        Effect = "Allow"
        Principal = {
          AWS = var.consumer_lambda_role_arns
        }
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
          "sqs:GetQueueUrl",
          "sqs:ChangeMessageVisibility",
          # DLQ replay tooling needs SendMessage to push events back
          # onto the main queue. No production Lambda should grant
          # itself this on the DLQ — list it here only for explicit
          # operator-tooling roles.
        ]
        Resource = aws_sqs_queue.cdc_dlq.arn
      },
    ]
  })
}

###############################################################################
# Placeholder Python source tree.
#
# A `terraform apply` of this module on a fresh checkout must succeed
# even though services/cdc-reader/ does not exist yet. The module ships
# a checked-in placeholder source tree at
# `${path.module}/cdc-reader-placeholder/` — the packager points at it
# whenever `var.cdc_reader_source_dir` is null.
#
# The placeholder handler logs a warning and returns a structured
# "not implemented" payload — it deliberately does NOT consume from
# the slot or write to SQS, so wiring this module today does no harm
# even if the CDC pipeline is incomplete. Replace the placeholder by
# setting `var.cdc_reader_source_dir = "${path.root}/../../../services/cdc-reader"`
# once Task 18.x lands the production handler.
###############################################################################

###############################################################################
# Package builder — pip install + copy handler.py.
###############################################################################

resource "null_resource" "package" {
  triggers = {
    source_hash       = local.source_hash
    python_executable = var.python_executable
    build_dir         = local.build_dir
    source_dir        = local.effective_source_dir
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    environment = {
      SOURCE_DIR  = local.effective_source_dir
      PACKAGE_DIR = local.package_dir
      PYTHON_BIN  = var.python_executable
    }
    command = <<-EOT
      set -euo pipefail

      # Wipe and recreate the staging dir so a previous failed build
      # cannot pollute the next zip.
      rm -rf "$PACKAGE_DIR"
      mkdir -p "$PACKAGE_DIR"

      # Optional: install runtime deps if the source tree has a
      # requirements.txt. The placeholder ships an empty file so this
      # is a fast no-op until services/cdc-reader/ lands with real deps
      # (psycopg[binary], etc.).
      if [ -f "$SOURCE_DIR/requirements.txt" ]; then
        if [ -s "$SOURCE_DIR/requirements.txt" ] && grep -qv '^#' "$SOURCE_DIR/requirements.txt"; then
          "$PYTHON_BIN" -m pip install \
            --quiet \
            --no-compile \
            --target "$PACKAGE_DIR" \
            --requirement "$SOURCE_DIR/requirements.txt"
        fi
      fi

      # Copy every .py file from the source tree (excluding tests/).
      find "$SOURCE_DIR" -maxdepth 4 -name '*.py' -not -path '*/tests/*' -print0 | \
        while IFS= read -r -d '' f; do
          rel="$${f#"$SOURCE_DIR"/}"
          dst="$PACKAGE_DIR/$rel"
          mkdir -p "$(dirname "$dst")"
          cp -p "$f" "$dst"
        done

      # Strip pip-installed __pycache__ to shave bytes off the zip.
      find "$PACKAGE_DIR" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
    EOT
  }
}

data "archive_file" "package" {
  type        = "zip"
  source_dir  = local.package_dir
  output_path = local.zip_path

  depends_on = [null_resource.package]
}

###############################################################################
# IAM — CDC Reader Lambda execution role.
###############################################################################

resource "aws_iam_role" "cdc_reader_exec" {
  name = "${local.cdc_reader_function_name}-exec"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "lambda.amazonaws.com" }
        Action    = "sts:AssumeRole"
      },
    ]
  })

  tags = merge(local.common_tags, {
    Name = "${local.cdc_reader_function_name}-exec"
  })
}

resource "aws_iam_role_policy_attachment" "cdc_reader_vpc" {
  role       = aws_iam_role.cdc_reader_exec.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

# Aurora IAM database authentication — scoped to ONE cluster + ONE DB
# user. The Lambda mints a short-lived token with
# `boto3.client('rds').generate_db_auth_token` and uses it as the
# password when opening the replication connection. No long-lived
# credentials in env vars.
resource "aws_iam_role_policy" "cdc_reader_rds_db_connect" {
  name = "${local.cdc_reader_function_name}-rds-db-connect"
  role = aws_iam_role.cdc_reader_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = "rds-db:connect"
        Resource = format(
          "arn:%s:rds-db:%s:%s:dbuser:%s/%s",
          data.aws_partition.current.partition,
          data.aws_region.current.name,
          data.aws_caller_identity.current.account_id,
          var.aurora_cluster_resource_id,
          var.aurora_db_user_for_cdc,
        )
      },
    ]
  })
}

# Send to the main FIFO queue. Decryption is required when a CMK is
# attached so the Lambda can encrypt outbound payloads.
resource "aws_iam_role_policy" "cdc_reader_sqs_send" {
  name = "${local.cdc_reader_function_name}-sqs-send"
  role = aws_iam_role.cdc_reader_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        {
          Effect = "Allow"
          Action = [
            "sqs:SendMessage",
            "sqs:GetQueueAttributes",
            "sqs:GetQueueUrl",
          ]
          Resource = aws_sqs_queue.cdc_main.arn
        },
      ],
      # Include KMS perms only when a CMK was supplied — the AWS-managed
      # `alias/aws/sqs` key grants implicit access via the SQS service.
      var.sqs_kms_key_arn != null ? [
        {
          Effect = "Allow"
          Action = [
            "kms:GenerateDataKey",
            "kms:Decrypt",
          ]
          Resource = var.sqs_kms_key_arn
        },
      ] : [],
    )
  })
}

###############################################################################
# CloudWatch Logs group.
###############################################################################

resource "aws_cloudwatch_log_group" "cdc_reader" {
  name              = local.cdc_reader_log_group_name
  retention_in_days = var.cdc_reader_log_retention_days

  tags = merge(local.common_tags, {
    Name = local.cdc_reader_log_group_name
  })
}

###############################################################################
# CDC Reader Lambda.
###############################################################################

resource "aws_lambda_function" "cdc_reader" {
  function_name = local.cdc_reader_function_name
  role          = aws_iam_role.cdc_reader_exec.arn

  runtime = "python3.12"
  handler = "handler.handler"

  filename         = data.archive_file.package.output_path
  source_code_hash = data.archive_file.package.output_base64sha256

  memory_size = var.cdc_reader_memory_mb
  timeout     = var.cdc_reader_timeout_seconds

  vpc_config {
    subnet_ids         = var.vpc_subnet_ids
    security_group_ids = var.vpc_security_group_ids
  }

  kms_key_arn = var.lambda_kms_key_arn

  environment {
    variables = {
      DB_HOST            = var.aurora_cluster_endpoint
      DB_PORT            = tostring(var.db_port)
      DB_NAME            = var.db_name
      DB_USER            = var.aurora_db_user_for_cdc
      CDC_SLOT_NAME      = var.cdc_replication_slot_name
      CDC_PUBLICATION    = var.cdc_publication_name
      CDC_QUEUE_URL      = aws_sqs_queue.cdc_main.url
      CDC_QUEUE_ARN      = aws_sqs_queue.cdc_main.arn
      CDC_DLQ_URL        = aws_sqs_queue.cdc_dlq.url
      AURORA_RESOURCE_ID = var.aurora_cluster_resource_id
      LOG_LEVEL          = var.cdc_reader_log_level
    }
  }

  tags = merge(local.common_tags, {
    Name = local.cdc_reader_function_name
  })

  depends_on = [
    aws_cloudwatch_log_group.cdc_reader,
    aws_iam_role_policy_attachment.cdc_reader_vpc,
    aws_iam_role_policy.cdc_reader_rds_db_connect,
    aws_iam_role_policy.cdc_reader_sqs_send,
  ]
}

###############################################################################
# EventBridge Scheduler — fires the CDC Reader on a fixed cadence.
#
# We use `aws_scheduler_schedule` (the EventBridge Scheduler service)
# rather than the older `aws_cloudwatch_event_rule` because Scheduler
# is the AWS-recommended cron/rate engine for new workloads (lower
# limits, easier IAM, supports `flexible_time_window`). The cadence is
# `var.cdc_reader_schedule_expression` — default `rate(1 minute)`.
###############################################################################

resource "aws_iam_role" "scheduler" {
  name = "${local.cdc_reader_function_name}-scheduler"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "scheduler.amazonaws.com" }
        Action    = "sts:AssumeRole"
      },
    ]
  })

  tags = merge(local.common_tags, {
    Name = "${local.cdc_reader_function_name}-scheduler"
  })
}

resource "aws_iam_role_policy" "scheduler_invoke" {
  name = "${local.cdc_reader_function_name}-scheduler-invoke"
  role = aws_iam_role.scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = aws_lambda_function.cdc_reader.arn
      },
    ]
  })
}

resource "aws_scheduler_schedule" "cdc_reader" {
  name = local.scheduler_name

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression = var.cdc_reader_schedule_expression

  target {
    arn      = aws_lambda_function.cdc_reader.arn
    role_arn = aws_iam_role.scheduler.arn

    # Empty payload — the handler reads everything from environment vars.
    input = jsonencode({
      source = "scheduled"
    })

    retry_policy {
      maximum_event_age_in_seconds = 300
      maximum_retry_attempts       = 0 # The Lambda is idempotent and the next tick will retry; no per-event Scheduler retries needed.
    }
  }

  depends_on = [aws_iam_role_policy.scheduler_invoke]
}

###############################################################################
# CloudWatch alarm — DLQ depth > 0.
#
# The CDC pipeline is "1 message in DLQ = something is wrong" by design
# (R28.6). The default threshold is 0, evaluation_periods 1, period 60s
# — i.e. as soon as a message lands in the DLQ for one minute, the
# alarm goes ALARM and pages whoever is subscribed (when an SNS topic
# is supplied via `var.dlq_alarm_actions`).
###############################################################################

resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  alarm_name        = local.dlq_alarm_name
  alarm_description = "Allen BioData Registry CDC pipeline DLQ has at least one message — investigate ${local.queue_dlq_name}."

  namespace   = "AWS/SQS"
  metric_name = "ApproximateNumberOfMessagesVisible"
  statistic   = "Maximum"
  period      = 60

  evaluation_periods  = var.dlq_alarm_evaluation_periods
  threshold           = var.dlq_alarm_threshold
  comparison_operator = "GreaterThanThreshold"

  dimensions = {
    QueueName = aws_sqs_queue.cdc_dlq.name
  }

  treat_missing_data = "notBreaching"

  alarm_actions = var.dlq_alarm_actions
  ok_actions    = var.dlq_alarm_actions

  tags = merge(local.common_tags, {
    Name = local.dlq_alarm_name
  })
}

###############################################################################
# (Reserved) EventBridge Pipes — Aurora source.
#
# Documented as a reference for the production hardening path. AWS
# EventBridge Pipes does NOT currently support a PostgreSQL logical
# replication slot as a native source — the supported sources are DDB
# Streams, Kinesis, MSK, SQS, RabbitMQ/MQ, and the Self-Managed Apache
# Kafka source. A "Pipe from Aurora" therefore needs an upstream relay:
# either MSK + Debezium (the canonical Kafka-based path) or AWS DMS
# with a Kinesis target. We park the resource block here as commented
# scaffolding so the diff to the real implementation is small once the
# upstream relay is chosen.
#
# resource "aws_pipes_pipe" "aurora_to_sqs" {
#   name     = "${var.name_prefix}-aurora-to-sqs"
#   role_arn = aws_iam_role.pipes_exec.arn
#
#   # `source` would be the MSK/Kinesis/DMS endpoint, not the Aurora
#   # cluster directly. Concrete schema depends on the chosen relay.
#   source = "<msk-cluster-arn-or-kinesis-stream-arn>"
#
#   target = aws_sqs_queue.cdc_main.arn
#
#   target_parameters {
#     sqs_queue_parameters {
#       message_group_id          = "$.detail.table_name-$.detail.pk"
#       message_deduplication_id  = "$.detail.lsn"
#     }
#   }
# }
#
# Until the upstream relay decision is made, the CDC Reader Lambda above
# performs the slot-drain-to-SQS function inline. The Indexing_Lambda
# (Task 18.1) sees the same SQS contract either way, so the swap is
# transparent at the consumer.
###############################################################################

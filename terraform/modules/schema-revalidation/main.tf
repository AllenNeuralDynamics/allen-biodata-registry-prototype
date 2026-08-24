###############################################################################
# Allen BioData Registry PoC — schema-revalidation module.
#
# Provisions the asynchronous schema revalidation pipeline:
#
#   EventBridge rule (schema.version.published)
#       │
#       ▼
#   SQS queue: schema-revalidation-queue (+ DLQ)
#       │
#       ▼
#   Revalidation_Lambda  (consumes via event source mapping)
#       │
#       ├── pages data_asset rows by schema_id
#       ├── enqueues per-asset revalidation tasks (back to itself
#       │   through the same queue with action='revalidate_asset'
#       │   in the message body)
#       └── runs aind-data-schema validation, writes
#           validation_status / validation_errors, and creates an
#           entity_revision row with change_source='ETL'.
#
# Validates: R5.3, R5.4 | Design: §IaC.Terraform Modules (`schema-revalidation`)
###############################################################################

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 5.0" }
    archive = { source = "hashicorp/archive", version = "~> 2.4" }
    null    = { source = "hashicorp/null", version = "~> 3.2" }
  }
}

locals {
  name_prefix       = var.name_prefix
  function_suffix   = "revalidation"
  function_name     = "${local.name_prefix}-${local.function_suffix}"
  queue_name        = "${local.name_prefix}-schema-revalidation"
  dlq_name          = "${local.name_prefix}-schema-revalidation-dlq"
  rule_name         = "${local.name_prefix}-schema-version-published"
  event_bus_name    = var.event_bus_name
  event_source      = "biodata-registry.schemas"
  event_detail_type = "schema.version.published"
  common_tags = merge(
    {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      Module      = "schema-revalidation"
    },
    var.tags,
  )
}

###############################################################################
# SQS — main queue + DLQ. The DLQ catches messages that exceed the
# maxReceiveCount on the main queue (poison messages).
###############################################################################

resource "aws_sqs_queue" "dlq" {
  name                      = local.dlq_name
  message_retention_seconds = 1209600 # 14 days
  sqs_managed_sse_enabled   = true
  tags                      = local.common_tags
}

resource "aws_sqs_queue" "main" {
  name = local.queue_name
  # Standard queue (not FIFO): per-asset revalidation has no ordering
  # requirement. Idempotent re-runs are safe because each task ends
  # with an UPSERT-style write keyed on (entity_type, entity_id).
  visibility_timeout_seconds = max(var.timeout_seconds * 6, 180)
  message_retention_seconds  = 345600 # 4 days
  # SSE-SQS (AWS-owned key) is used instead of SSE-KMS with the AWS-managed
  # `aws/sqs` key because the latter does not grant EventBridge permission
  # to encrypt messages on the way in. Customer-managed KMS keys would
  # require an explicit grant — overkill for the PoC.
  sqs_managed_sse_enabled = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = var.max_receive_count
  })

  tags = local.common_tags
}

###############################################################################
# EventBridge — rule routes schema.version.published events to the queue.
# The publishing call site is Validation_Lambda (Task 21.1) when a new
# schema version is created.
###############################################################################

resource "aws_cloudwatch_event_rule" "schema_published" {
  name           = local.rule_name
  description    = "Routes schema.version.published events to the revalidation queue."
  event_bus_name = local.event_bus_name

  event_pattern = jsonencode({
    source      = [local.event_source]
    detail-type = [local.event_detail_type]
  })

  tags = local.common_tags
}

resource "aws_cloudwatch_event_target" "schema_published_to_queue" {
  rule           = aws_cloudwatch_event_rule.schema_published.name
  event_bus_name = local.event_bus_name
  arn            = aws_sqs_queue.main.arn
  target_id      = "schema-revalidation-queue"

  # Forward the event detail directly. The handler unpacks {schema_id,
  # version_id, action: "schema_published"} and fans out per-asset tasks.
  input_transformer {
    input_paths = {
      schema_id  = "$.detail.schema_id"
      version_id = "$.detail.version_id"
    }
    input_template = <<EOT
{
  "action": "schema_published",
  "schema_id": <schema_id>,
  "version_id": <version_id>
}
EOT
  }
}

# Grant EventBridge permission to publish to the SQS queue.
resource "aws_sqs_queue_policy" "main" {
  queue_url = aws_sqs_queue.main.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowEventBridgePublish"
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.main.arn
      Condition = {
        ArnEquals = {
          "aws:SourceArn" = aws_cloudwatch_event_rule.schema_published.arn
        }
      }
    }]
  })
}

###############################################################################
# Revalidation_Lambda — uses the shared `business` Lambda module to inherit
# the standard Aurora IAM auth, VPC config, source-hash repackaging, and
# log-group conventions. Custom IAM statements are added below for the
# Lambda's permission to consume from SQS and (recursively) re-enqueue
# per-asset tasks back into the same queue.
###############################################################################

module "lambda" {
  source = "../lambdas/business"

  name_prefix       = var.name_prefix
  function_suffix   = local.function_suffix
  environment       = var.environment
  project           = var.project
  region            = var.region
  python_executable = var.python_executable
  source_dir        = var.source_dir
  shared_layer_arn  = var.shared_layer_arn

  aurora_host                = var.aurora_host
  aurora_port                = var.aurora_port
  aurora_db_name             = var.aurora_db_name
  db_user                    = var.db_user
  aurora_cluster_resource_id = var.aurora_cluster_resource_id

  subnet_ids         = var.subnet_ids
  security_group_ids = var.security_group_ids

  memory_mb       = var.memory_mb
  timeout_seconds = var.timeout_seconds
  log_level       = var.log_level

  extra_environment = {
    REVALIDATION_QUEUE_URL = aws_sqs_queue.main.url
    REVALIDATION_BATCH     = tostring(var.per_asset_batch_size)
  }

  extra_iam_statements = [
    {
      Effect = "Allow"
      Action = [
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes",
        "sqs:ChangeMessageVisibility",
        "sqs:SendMessage", # required for self-enqueue of per-asset tasks
      ]
      Resource = aws_sqs_queue.main.arn
    },
    {
      Effect = "Allow"
      Action = ["sqs:SendMessage"]
      Resource = aws_sqs_queue.dlq.arn
    },
  ]

  tags = var.tags
}

###############################################################################
# Event source mapping — wires the SQS queue to the Lambda.
###############################################################################

resource "aws_lambda_event_source_mapping" "sqs" {
  event_source_arn = aws_sqs_queue.main.arn
  function_name    = module.lambda.function_arn

  batch_size                         = var.batch_size
  maximum_batching_window_in_seconds = 5

  enabled = true

  depends_on = [module.lambda]
}

###############################################################################
# CloudWatch — publish queue depth alarm. R5.3: queue depth must be
# observable so operators can detect a stuck schema-published event.
###############################################################################

resource "aws_cloudwatch_metric_alarm" "queue_depth" {
  alarm_name        = "${local.queue_name}-depth-high"
  alarm_description = "Schema revalidation queue depth exceeds threshold; investigate Lambda errors or throttling."

  namespace   = "AWS/SQS"
  metric_name = "ApproximateNumberOfMessagesVisible"
  statistic   = "Maximum"
  period      = 300

  evaluation_periods  = 2
  threshold           = var.queue_depth_alarm_threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.main.name
  }

  tags = local.common_tags
}

resource "aws_cloudwatch_metric_alarm" "dlq_messages" {
  alarm_name        = "${local.dlq_name}-has-messages"
  alarm_description = "Schema revalidation DLQ contains messages; some revalidations failed irrecoverably."

  namespace   = "AWS/SQS"
  metric_name = "ApproximateNumberOfMessagesVisible"
  statistic   = "Maximum"
  period      = 300

  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.dlq.name
  }

  tags = local.common_tags
}

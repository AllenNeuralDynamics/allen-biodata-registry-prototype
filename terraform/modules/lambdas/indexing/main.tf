###############################################################################
# Allen BioData Registry PoC — lambdas/indexing module
#
# Provisions the CDC consumer Lambda (Task 18.1) and wires it to the
# SQS FIFO queue produced by the cdc-pipeline module:
#
#   cdc-pipeline.main_queue (FIFO) ──[event source mapping]──> Indexing_Lambda
#                                                                  │
#                                                                  ├──> DocumentDB (replace_one upsert)
#                                                                  ├──> OpenSearch (index)
#                                                                  └──> DLQ (target-tagged failures)
#
# The Lambda packages services/indexing-lambda/handler.py plus the
# runtime deps from requirements.txt (psycopg, pymongo, opensearch-py,
# requests-aws4auth) into a deployment zip via `archive_file`.
#
# IAM scoping (R32.4 — least privilege per Lambda):
#   * VPC ENI mgmt + CloudWatch Logs (managed AWSLambdaVPCAccessExecutionRole).
#   * `secretsmanager:GetSecretValue` on the Aurora + DocumentDB secrets.
#   * `aoss:APIAccessAll` on the OpenSearch Serverless collection ARN.
#   * `sqs:ReceiveMessage`/`DeleteMessage`/`GetQueueAttributes` on the
#     source SQS queue.
#   * `sqs:SendMessage` on the DLQ.
#   * `kms:Decrypt` on the Aurora + DocumentDB CMKs (when supplied).
#
# Validates: R1.7, R8.4, R17.9, R28.3, R28.4, R28.5, R28.6.
#
# Design references:
#   * design.md §Components.12. Indexing_Lambda.
#   * design.md §Architecture.CDC Pipeline Architecture.
#   * design.md §IaC.Terraform Modules (`lambdas/indexing`).
###############################################################################

locals {
  common_tags = merge(
    {
      Project     = var.project
      Environment = var.environment
      Module      = "lambdas/indexing"
      ManagedBy   = "terraform"
    },
    var.tags,
  )

  function_name = "${var.name_prefix}-indexing"

  # Per-module-instance build directory under the calling root's
  # `.terraform` cache, so multiple compositions don't clobber each
  # other's builds.
  build_dir   = coalesce(var.build_dir, "${path.root}/.terraform/biodata-registry/${var.name_prefix}-indexing-build")
  package_dir = "${local.build_dir}/package"
  zip_path    = "${local.build_dir}/indexing.zip"

  # Python source files we hash to drive package rebuilds. Tests are
  # excluded — they don't ship in the deployment image.
  source_py_files = fileset(var.source_dir, "**/*.py")
  source_hash = sha256(join("|", concat(
    [
      for f in local.source_py_files :
      filesha256("${var.source_dir}/${f}")
      if !startswith(f, "tests/")
    ],
    [
      # requirements.txt — fileset returns [] if absent.
      for f in fileset(var.source_dir, "requirements.txt") :
      filesha256("${var.source_dir}/${f}")
    ],
  )))

  layers = var.shared_layer_arn == null ? [] : [var.shared_layer_arn]
}

data "aws_partition" "current" {}
data "aws_caller_identity" "current" {}

###############################################################################
# Package builder — pip install runtime deps + copy handler.py.
###############################################################################

resource "null_resource" "package" {
  triggers = {
    source_hash       = local.source_hash
    python_executable = var.python_executable
    build_dir         = local.build_dir
    source_dir        = var.source_dir
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    environment = {
      SOURCE_DIR  = var.source_dir
      PACKAGE_DIR = local.package_dir
      PYTHON_BIN  = var.python_executable
    }
    command = <<-EOT
      set -euo pipefail

      # Wipe and recreate the staging dir so a previous failed build
      # cannot pollute the next zip.
      rm -rf "$PACKAGE_DIR"
      mkdir -p "$PACKAGE_DIR"

      # Install runtime deps (psycopg, pymongo, opensearch-py, requests-aws4auth).
      # We force Linux x86_64 wheels via --platform so the resulting zip
      # runs on Lambda regardless of the developer's host OS. Without
      # these flags, pip on macOS pulls macOS wheels for psycopg_binary
      # and the Lambda fails at import with "no pq wrapper available".
      if [ -f "$SOURCE_DIR/requirements.txt" ]; then
        if [ -s "$SOURCE_DIR/requirements.txt" ] && grep -qv '^#' "$SOURCE_DIR/requirements.txt"; then
          "$PYTHON_BIN" -m pip install \
            --quiet \
            --no-compile \
            --platform manylinux2014_x86_64 \
            --only-binary=:all: \
            --python-version 3.12 \
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
# IAM execution role.
###############################################################################

resource "aws_iam_role" "exec" {
  name = "${local.function_name}-exec"

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
    Name = "${local.function_name}-exec"
  })
}

# VPC ENI management + CloudWatch Logs — managed policy.
resource "aws_iam_role_policy_attachment" "vpc" {
  role       = aws_iam_role.exec.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

# Secrets Manager — read the Aurora + DocumentDB credentials.
resource "aws_iam_role_policy" "secrets" {
  name = "${local.function_name}-secrets"
  role = aws_iam_role.exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret",
        ]
        Resource = [
          var.aurora_secret_arn,
          var.docdb_secret_arn,
        ]
      },
    ]
  })
}

# OpenSearch Serverless — collection-scoped data-plane access.
resource "aws_iam_role_policy" "opensearch" {
  name = "${local.function_name}-opensearch"
  role = aws_iam_role.exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "aoss:APIAccessAll"
        Resource = var.opensearch_collection_arn
      },
    ]
  })
}

# SQS — receive from the cdc-pipeline source queue, send-only on the DLQ.
resource "aws_iam_role_policy" "sqs" {
  name = "${local.function_name}-sqs"
  role = aws_iam_role.exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
          "sqs:GetQueueUrl",
          "sqs:ChangeMessageVisibility",
        ]
        Resource = var.source_sqs_queue_arn
      },
      {
        Effect = "Allow"
        Action = [
          "sqs:SendMessage",
          "sqs:GetQueueAttributes",
          "sqs:GetQueueUrl",
        ]
        Resource = var.dlq_arn
      },
    ]
  })
}

# KMS — Decrypt on the Aurora + DocumentDB CMKs.
#
# Always emitted in the dev composition; both Aurora and DocumentDB modules
# always create a customer-managed key (R31.3). The `count = length(...) > 0`
# pattern was unsafe because the KMS ARNs are module outputs not known until
# apply, breaking Terraform's count evaluation.
locals {
  kms_arns = compact([
    var.aurora_kms_key_arn,
    var.docdb_kms_key_arn,
  ])
}

resource "aws_iam_role_policy" "kms" {
  name = "${local.function_name}-kms"
  role = aws_iam_role.exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:DescribeKey",
        ]
        Resource = local.kms_arns
      },
    ]
  })
}

###############################################################################
# CloudWatch Logs group.
###############################################################################

resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = var.log_retention_days

  tags = merge(local.common_tags, {
    Name = "/aws/lambda/${local.function_name}"
  })
}

###############################################################################
# Lambda function.
###############################################################################

resource "aws_lambda_function" "this" {
  function_name = local.function_name
  role          = aws_iam_role.exec.arn

  runtime = "python3.12"
  handler = "handler.lambda_handler"

  filename         = data.archive_file.package.output_path
  source_code_hash = data.archive_file.package.output_base64sha256

  memory_size = var.memory_mb
  timeout     = var.timeout_seconds

  reserved_concurrent_executions = var.reserved_concurrency

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = var.security_group_ids
  }

  layers = local.layers

  kms_key_arn = var.lambda_kms_key_arn

  environment {
    variables = {
      AURORA_SECRET_ARN   = var.aurora_secret_arn
      AURORA_HOST         = var.aurora_host
      AURORA_PORT         = tostring(var.aurora_port)
      AURORA_DB           = var.aurora_db_name == null ? "" : var.aurora_db_name
      DOCDB_SECRET_ARN    = var.docdb_secret_arn
      DOCDB_ENDPOINT      = var.docdb_endpoint
      DOCDB_PORT          = tostring(var.docdb_port)
      OPENSEARCH_ENDPOINT = var.opensearch_endpoint
      OPENSEARCH_REGION   = var.region
      DLQ_URL             = var.dlq_url
      LOG_LEVEL           = var.log_level
    }
  }

  tags = merge(local.common_tags, {
    Name = local.function_name
  })

  # Make sure the log group exists before the first invocation so
  # CloudWatch does not auto-create a 'Never expire' group as a side
  # effect of the first invocation.
  depends_on = [
    aws_cloudwatch_log_group.this,
    aws_iam_role_policy_attachment.vpc,
    aws_iam_role_policy.secrets,
    aws_iam_role_policy.opensearch,
    aws_iam_role_policy.sqs,
  ]
}

###############################################################################
# Event source mapping — wire the SQS FIFO queue to the Lambda.
#
# `function_response_types = ["ReportBatchItemFailures"]` would let the
# Lambda partially-fail a batch by returning per-message item ids. We
# don't use it because the handler itself catches per-target failures
# and routes them to the DLQ — by the time control returns from the
# handler, every record has either been processed or DLQ'd. Returning
# the partial-failure list would double-DLQ messages.
###############################################################################

resource "aws_lambda_event_source_mapping" "sqs" {
  event_source_arn = var.source_sqs_queue_arn
  function_name    = aws_lambda_function.this.arn

  batch_size = var.batch_size

  # NOTE: maximum_batching_window_in_seconds is intentionally omitted.
  # FIFO SQS queues do NOT support batching windows (Lambda API rejects
  # the parameter). The queue's own visibility timeout + Lambda's polling
  # loop produces the equivalent batching behavior for FIFO sources.

  # Required to be true for FIFO sources where strict ordering
  # matters — but in this case ordering is enforced by the queue's
  # MessageGroupId (table:pk), not by the event source mapping.
  enabled = true

  depends_on = [aws_iam_role_policy.sqs]
}

###############################################################################
# CloudWatch alarms — invocation errors + DLQ depth.
###############################################################################

resource "aws_cloudwatch_metric_alarm" "errors" {
  alarm_name        = "${local.function_name}-errors"
  alarm_description = "Indexing_Lambda invocation errors. Investigate the Lambda log group ${aws_cloudwatch_log_group.this.name}."

  namespace   = "AWS/Lambda"
  metric_name = "Errors"
  statistic   = "Sum"
  period      = 300

  evaluation_periods  = var.error_alarm_evaluation_periods
  threshold           = var.error_alarm_threshold
  comparison_operator = "GreaterThanThreshold"

  dimensions = {
    FunctionName = aws_lambda_function.this.function_name
  }

  treat_missing_data = "notBreaching"

  alarm_actions = var.alarm_actions
  ok_actions    = var.alarm_actions

  tags = merge(local.common_tags, {
    Name = "${local.function_name}-errors"
  })
}

# DLQ depth alarm — the Indexing Lambda's contract is "any message in
# the DLQ means a target write failed". We alarm on >0 messages over a
# single 60-second period for high-signal pages.
resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  alarm_name        = "${local.function_name}-dlq-not-empty"
  alarm_description = "Indexing_Lambda DLQ has at least one message — investigate via DLQ_URL ${var.dlq_url}."

  namespace   = "AWS/SQS"
  metric_name = "ApproximateNumberOfMessagesVisible"
  statistic   = "Maximum"
  period      = 60

  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  dimensions = {
    # SQS metric dimension wants the queue *name*, not its ARN. Parse
    # the trailing component of the ARN (FIFO queues end in `.fifo`).
    QueueName = element(split(":", var.dlq_arn), length(split(":", var.dlq_arn)) - 1)
  }

  treat_missing_data = "notBreaching"

  alarm_actions = var.alarm_actions
  ok_actions    = var.alarm_actions

  tags = merge(local.common_tags, {
    Name = "${local.function_name}-dlq-not-empty"
  })
}

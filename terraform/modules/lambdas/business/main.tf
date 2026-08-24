###############################################################################
# Allen BioData Registry PoC — generic business Lambda module.
#
# Stamps out the standard pattern shared by all the Phase 3-5 business
# Lambdas: VPC config, RDS IAM auth on Aurora, scoped IAM execution role,
# CloudWatch log group, source-hash-driven repackaging, optional shared
# Lambda Layer attachment.
#
# Used by: validation, lifecycle, duplicates, governance, revisions,
#          collections, observability, embedding-backfill, metadata-agent.
#
# Each consumer module instantiates this module once with a different
# `function_suffix` and (optionally) extra IAM policies via
# `extra_iam_statements`.
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
  function_name = "${var.name_prefix}-${var.function_suffix}"
  build_dir     = coalesce(var.build_dir, "${path.root}/.terraform/biodata-registry/${local.function_name}-build")
  package_dir   = "${local.build_dir}/package"
  zip_path      = "${local.build_dir}/handler.zip"

  source_hash = sha1(join("", concat(
    [for f in fileset(var.source_dir, "**/*") : filesha1("${var.source_dir}/${f}")],
    fileexists("${var.source_dir}/../_lambda_common.py") ? [filesha1("${var.source_dir}/../_lambda_common.py")] : [],
  )))

  common_tags = merge(
    {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      Lambda      = local.function_name
    },
    var.tags,
  )
}

resource "null_resource" "package" {
  triggers = {
    source_hash       = local.source_hash
    build_dir         = local.build_dir
    python_executable = var.python_executable
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

      rm -rf "$PACKAGE_DIR"
      mkdir -p "$PACKAGE_DIR"

      if [ -f "$SOURCE_DIR/requirements.txt" ] && grep -qv '^#' "$SOURCE_DIR/requirements.txt"; then
        "$PYTHON_BIN" -m pip install \
          --quiet --no-compile \
          --platform manylinux2014_x86_64 \
          --only-binary=:all: \
          --python-version 3.12 \
          --target "$PACKAGE_DIR" \
          --requirement "$SOURCE_DIR/requirements.txt"
      fi

      cp "$SOURCE_DIR/handler.py" "$PACKAGE_DIR/handler.py"

      # Copy the shared _lambda_common.py into the package so the handler
      # can `from _lambda_common import ...`. The file lives one level up
      # from each business Lambda's source dir (i.e. services/_lambda_common.py).
      if [ -f "$SOURCE_DIR/../_lambda_common.py" ]; then
        cp "$SOURCE_DIR/../_lambda_common.py" "$PACKAGE_DIR/_lambda_common.py"
      fi

      find "$PACKAGE_DIR" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
    EOT
  }
}

data "archive_file" "package" {
  type        = "zip"
  source_dir  = local.package_dir
  output_path = local.zip_path
  depends_on  = [null_resource.package]
}

resource "aws_iam_role" "exec" {
  name = "${local.function_name}-exec"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "vpc" {
  role       = aws_iam_role.exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

# RDS IAM database authentication scoped to {cluster, db_user}.
resource "aws_iam_role_policy" "rds_db_connect" {
  count = var.enable_rds_iam_auth ? 1 : 0

  name = "${local.function_name}-rds-db-connect"
  role = aws_iam_role.exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "rds-db:connect"
      Resource = format(
        "arn:aws:rds-db:%s:%s:dbuser:%s/%s",
        var.region,
        data.aws_caller_identity.current.account_id,
        var.aurora_cluster_resource_id,
        var.db_user,
      )
    }]
  })
}

# Optional extra IAM statements (e.g. invoke-Bedrock, opensearch APIAccess,
# scheduler:CreateSchedule for embedding-backfill).
resource "aws_iam_role_policy" "extra" {
  count = length(var.extra_iam_statements) > 0 ? 1 : 0

  name = "${local.function_name}-extra"
  role = aws_iam_role.exec.id

  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = var.extra_iam_statements
  })
}

resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = var.log_retention_days
  tags              = local.common_tags
}

resource "aws_lambda_function" "this" {
  function_name    = local.function_name
  role             = aws_iam_role.exec.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.package.output_path
  source_code_hash = data.archive_file.package.output_base64sha256

  memory_size = var.memory_mb
  timeout     = var.timeout_seconds

  layers = compact([var.shared_layer_arn])

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = var.security_group_ids
  }

  environment {
    variables = merge(
      {
        AURORA_HOST                = var.aurora_host
        AURORA_PORT                = tostring(var.aurora_port)
        AURORA_DB                  = var.aurora_db_name
        AURORA_DB_USER             = var.db_user
        AURORA_CLUSTER_RESOURCE_ID = var.aurora_cluster_resource_id
        LOG_LEVEL                  = var.log_level
      },
      var.extra_environment,
    )
  }

  tags = local.common_tags

  depends_on = [
    aws_cloudwatch_log_group.this,
    aws_iam_role_policy_attachment.vpc,
  ]
}

data "aws_caller_identity" "current" {}

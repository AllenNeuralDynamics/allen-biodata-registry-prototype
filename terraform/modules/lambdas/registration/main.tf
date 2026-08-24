###############################################################################
# Allen BioData Registry PoC — lambdas/registration module
#
# Provisions:
#   * A Python 3.12 Lambda function packaged from
#     services/registration-lambda/. The packaging step copies handler.py
#     and the OpenAPI spec into a build directory and zips the result.
#     Runtime deps come from the shared Lambda Layer (Task 12.1) — this
#     deployment package itself is intentionally tiny so cold starts
#     stay fast.
#   * An IAM execution role with:
#       - AWSLambdaVPCAccessExecutionRole (ENI mgmt + CloudWatch Logs).
#       - A scoped inline policy granting `rds-db:connect` to ONE specific
#         {cluster_resource_id, db_user} tuple — Aurora's IAM database
#         authentication. No Secrets Manager grants, no master-password
#         path, by design (R31.4 spirit, R32.4).
#   * A CloudWatch Logs group with retention pinned via variable.
#   * VPC config so the Lambda can reach Aurora's private subnets.
#
# Validates: R1.1, R1.2, R1.4, R1.5, R1.6, R1.7, R2.4, R2.5, R2.6, R6.1,
#            R6.2, R28.2, R33.1, R33.2 — Registration_Lambda implements
#            the core CRUD and revision-anchoring contract.
#
# Design references:
#   * design.md §Components.2. Registration_Lambda.
#   * design.md §Architecture.RLS Enforcement Architecture.
#
# Wiring:
#   The apigateway module (Task 14.1) consumes `function_name` and
#   `invoke_arn` outputs to wire the route integrations + invoke
#   permissions. The OpenAPI spec is the source of truth for which
#   routes target this Lambda; the apigateway module derives the
#   integration list from the spec.
###############################################################################

locals {
  common_tags = merge(
    {
      Project     = var.project
      Environment = var.environment
      Module      = "lambdas/registration"
      ManagedBy   = "terraform"
    },
    var.tags,
  )

  function_name = "${var.name_prefix}-registration"

  # If the caller did not provide a build directory, stash the staging
  # tree under the calling Terraform working directory's `.terraform`
  # cache. Doing this per-module-instance keeps multiple compositions
  # from clobbering each other's builds.
  build_dir   = coalesce(var.build_dir, "${path.root}/.terraform/biodata-registry/${var.name_prefix}-registration-build")
  package_dir = "${local.build_dir}/package"
  zip_path    = "${local.build_dir}/registration.zip"

  # Source files we hash to decide when the package needs rebuilding.
  source_files = fileset(var.source_dir, "**/*.py")

  # The OpenAPI spec is bundled into the deployment package so the
  # Lambda can validate requests without an extra runtime fetch.
  openapi_spec_path = coalesce(var.openapi_spec_path, "${var.source_dir}/../../openapi/openapi.yaml")

  layers = var.shared_layer_arn == null ? [] : [var.shared_layer_arn]
}

data "aws_partition" "current" {}
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

###############################################################################
# Source-tree hash — drives package rebuilds.
#
# Hash every .py file under the source directory (excluding tests/) and
# the OpenAPI spec. Any change in either bumps the hash and triggers
# null_resource.package to rebuild on next apply.
###############################################################################

locals {
  source_hash = sha256(join("|", concat(
    [
      for f in local.source_files :
      filesha256("${var.source_dir}/${f}")
      if !startswith(f, "tests/")
    ],
    [filesha256(local.openapi_spec_path)],
  )))
}

###############################################################################
# Package builder — copy handler.py + openapi.yaml into the staging dir.
#
# The shared Layer ships psycopg / aind-data-schema / openapi-core, so
# this Lambda doesn't pip-install anything itself. Skipping pip keeps
# the deployment zip small (a few KB) and the cold start fast.
###############################################################################

resource "null_resource" "package" {
  triggers = {
    source_hash = local.source_hash
    build_dir   = local.build_dir
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    environment = {
      SOURCE_DIR        = var.source_dir
      PACKAGE_DIR       = local.package_dir
      OPENAPI_SPEC_PATH = local.openapi_spec_path
    }
    command = <<-EOT
      set -euo pipefail

      # Wipe and recreate the staging dir so a previous failed build
      # cannot pollute the next zip with stale files.
      rm -rf "$PACKAGE_DIR"
      mkdir -p "$PACKAGE_DIR"

      # Copy the handler. We deliberately do NOT copy tests/ or
      # pyproject.toml — they belong only in the source tree, never in
      # the deployment image.
      cp "$SOURCE_DIR/handler.py" "$PACKAGE_DIR/handler.py"

      # Bundle the OpenAPI spec so the in-Lambda middleware can
      # validate requests without a runtime fetch.
      cp "$OPENAPI_SPEC_PATH" "$PACKAGE_DIR/openapi.yaml"

      # Strip pip-installed __pycache__ to shave a few KB off the zip
      # (defensive — there should be no __pycache__ in source_dir).
      find "$PACKAGE_DIR" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
    EOT
  }
}

###############################################################################
# Zip the staged package.
###############################################################################

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

# VPC ENI management + CloudWatch Logs.
resource "aws_iam_role_policy_attachment" "vpc" {
  role       = aws_iam_role.exec.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

# Aurora IAM database authentication — scoped to ONE cluster + ONE DB user.
# The resource ARN form
# `arn:aws:rds-db:<region>:<account>:dbuser:<cluster_resource_id>/<db_user>`
# is the documented IAM resource for `rds-db:connect`.
resource "aws_iam_role_policy" "rds_db_connect" {
  name = "${local.function_name}-rds-db-connect"
  role = aws_iam_role.exec.id

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
          var.db_user,
        )
      },
    ]
  })
}

###############################################################################
# CloudWatch Logs group — provisioned explicitly so retention is enforced.
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
  handler = "handler.handler"

  filename         = data.archive_file.package.output_path
  source_code_hash = data.archive_file.package.output_base64sha256

  memory_size = var.memory_mb
  timeout     = var.timeout_seconds

  vpc_config {
    subnet_ids         = var.vpc_subnet_ids
    security_group_ids = var.vpc_security_group_ids
  }

  layers = local.layers

  kms_key_arn = var.kms_key_arn

  environment {
    variables = {
      DB_HOST                    = var.aurora_host
      DB_PORT                    = tostring(var.aurora_port)
      DB_NAME                    = var.aurora_db_name
      DB_USER                    = var.db_user
      DB_SSLMODE                 = var.db_sslmode
      DB_CONNECT_TIMEOUT_SECONDS = tostring(var.db_connect_timeout_seconds)
      DB_STATEMENT_TIMEOUT_MS    = tostring(var.db_statement_timeout_ms)
      OPENAPI_SPEC_PATH          = "openapi.yaml"
      LOG_LEVEL                  = var.log_level
    }
  }

  tags = merge(local.common_tags, {
    Name = local.function_name
  })

  # Make sure the log group exists before the first invocation so
  # CloudWatch does not create a 'Never expire' group as a side effect.
  depends_on = [
    aws_cloudwatch_log_group.this,
    aws_iam_role_policy_attachment.vpc,
    aws_iam_role_policy.rds_db_connect,
  ]
}

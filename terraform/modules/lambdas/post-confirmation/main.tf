###############################################################################
# Allen BioData Registry PoC — lambdas/post-confirmation module
#
# Provisions:
#   * A Python 3.12 Lambda function packaged from
#     services/post-confirmation-lambda/. The packaging step pip-installs
#     the runtime deps (currently just pg8000) into a build directory,
#     copies handler.py alongside, and zips the result.
#   * An IAM execution role with:
#       - AWSLambdaVPCAccessExecutionRole (ENI mgmt + CloudWatch Logs).
#       - A scoped inline policy granting `rds-db:connect` to ONE specific
#         {cluster_resource_id, db_user} tuple — Aurora's IAM database
#         authentication. No Secrets Manager grants, no master-password
#         path, by design (R31.4 spirit, R32.4).
#   * A CloudWatch Logs group with retention pinned via variable.
#   * VPC config so the Lambda can reach Aurora's private subnets.
#
# Validates: R19.3 (the Lambda implementation that creates the `app_user`
# row when Cognito completes user confirmation; the cognito module wires
# the trigger configuration once `function_arn` is exported here).
#
# Design references:
#   * design.md §Components.User Onboarding Flow.
#   * design.md §Components.Lambda Functions (Cognito Post-Confirmation
#     Lambda counted separately from the 13 business Lambdas, owned by
#     the cognito module — but its source code and IAM live in this
#     standalone module so the cognito module stays focused on User Pool
#     configuration).
###############################################################################

locals {
  common_tags = merge(
    {
      Project     = var.project
      Environment = var.environment
      Module      = "lambdas/post-confirmation"
      ManagedBy   = "terraform"
    },
    var.tags,
  )

  function_name = "${var.name_prefix}-cognito-post-confirmation"

  # If the caller did not provide a build directory, stash the staging
  # tree under the calling Terraform working directory's `.terraform`
  # cache. Doing this per-module-instance keeps multiple compositions
  # from clobbering each other's builds.
  build_dir   = coalesce(var.build_dir, "${path.root}/.terraform/biodata-registry/${var.name_prefix}-post-confirmation-build")
  package_dir = "${local.build_dir}/package"
  zip_path    = "${local.build_dir}/post-confirmation.zip"

  # Source files we hash to decide when the package needs rebuilding.
  source_files           = fileset(var.source_dir, "**/*.py")
  requirements_file_path = "${var.source_dir}/requirements.txt"
}

data "aws_partition" "current" {}
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

###############################################################################
# Source-tree hash — drives package rebuilds.
#
# We hash the requirements file and every .py file under the source
# directory. Any change in either bumps the hash and triggers the
# null_resource to rebuild the package on next apply. Excludes tests
# (tests/) and pyproject.toml so test-only changes don't churn the
# Lambda image.
###############################################################################

locals {
  source_hash = sha256(join("|", concat(
    [filesha256(local.requirements_file_path)],
    [
      for f in local.source_files :
      filesha256("${var.source_dir}/${f}")
      if !startswith(f, "tests/")
    ],
  )))
}

###############################################################################
# Package builder — pip install + copy handler.py.
###############################################################################

resource "null_resource" "package" {
  triggers = {
    source_hash       = local.source_hash
    python_executable = var.python_executable
    build_dir         = local.build_dir
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
      # cannot pollute the next zip with stale files.
      rm -rf "$PACKAGE_DIR"
      mkdir -p "$PACKAGE_DIR"

      # Install runtime deps directly into the package root — Lambda
      # extracts the zip into its working directory, so deps need to
      # sit alongside handler.py.
      "$PYTHON_BIN" -m pip install \
        --quiet \
        --no-compile \
        --target "$PACKAGE_DIR" \
        --requirement "$SOURCE_DIR/requirements.txt"

      # Copy the handler. We deliberately do NOT copy tests/ or
      # pyproject.toml — they belong only in the source tree, never in
      # the deployment image.
      cp "$SOURCE_DIR/handler.py" "$PACKAGE_DIR/handler.py"

      # Strip pip-installed __pycache__ to shave a few KB off the zip.
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
# The resource ARN form `arn:aws:rds-db:<region>:<account>:dbuser:<cluster_resource_id>/<db_user>`
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
#
# Without this resource Lambda would auto-create the group with infinite
# retention on first invocation, which is both wasteful and a compliance
# foot-gun.
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

  kms_key_arn = var.kms_key_arn

  environment {
    variables = {
      DB_HOST             = var.db_host
      DB_PORT             = tostring(var.db_port)
      DB_NAME             = var.db_name
      DB_USER             = var.db_user
      APP_USER_HAS_ORG_ID = var.app_user_has_org_id ? "true" : "false"
      LOG_LEVEL           = var.log_level
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

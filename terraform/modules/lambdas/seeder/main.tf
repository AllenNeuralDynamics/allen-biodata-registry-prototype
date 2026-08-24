###############################################################################
# Allen BioData Registry PoC — lambdas/seeder module
#
# Provisions:
#   * A Python 3.12 Lambda function packaged from
#     services/seeder/. The packaging step pip-installs the runtime
#     deps (currently just pg8000) into a build directory, copies
#     handler.py + seeder.py + mapping.py alongside, and zips the
#     result.
#   * An IAM execution role with:
#       - AWSLambdaVPCAccessExecutionRole (ENI mgmt + CloudWatch Logs).
#       - A scoped inline policy granting `rds-db:connect` to ONE
#         specific {cluster_resource_id, db_user} tuple — Aurora's IAM
#         database authentication. Mirrors migration-runner.
#       - A scoped inline policy granting `s3:GetObject` on ONE
#         specific {seed_s3_bucket, seed_s3_key} tuple. The seeder
#         is read-only against S3; this is the smallest possible
#         grant. (R32.4)
#   * A CloudWatch Logs group with retention pinned via variable.
#   * VPC config so the Lambda can reach Aurora's private subnets.
#   * An `aws_lambda_invocation` resource that invokes the Lambda
#     synchronously on every `terraform apply` whose source-hash
#     trigger has changed. The invocation runs after the Lambda is
#     provisioned and treats a non-2xx response as a failed apply —
#     exactly the behavior we want for a bring-up-time seeder.
#
# Validates: R32.2 (sample data loaded), R32.5 (idempotent
# `terraform apply`).
#
# Design references:
#   * design.md §IaC.Idempotency and Sample Data
#   * design.md §Effort Estimation.Data Seeding
#   * services/seeder/README.md (Lambda-side documentation).
###############################################################################

locals {
  common_tags = merge(
    {
      Project     = var.project
      Environment = var.environment
      Module      = "lambdas/seeder"
      ManagedBy   = "terraform"
    },
    var.tags,
  )

  function_name = "${var.name_prefix}-seeder"

  build_dir   = coalesce(var.build_dir, "${path.root}/.terraform/biodata-registry/${var.name_prefix}-seeder-build")
  package_dir = "${local.build_dir}/package"
  zip_path    = "${local.build_dir}/seeder.zip"

  # Source files we hash to decide when the package needs rebuilding.
  # Scans every .py file under the source directory (excluding tests/)
  # plus the requirements file. The seeder does NOT bundle migrations
  # (that's the migration-runner's job); changes to migrations/*.sql
  # do not trigger a seeder rebuild.
  source_py_files        = fileset(var.source_dir, "**/*.py")
  requirements_file_path = "${var.source_dir}/requirements.txt"
}

data "aws_partition" "current" {}
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

###############################################################################
# Source-tree hash — drives package rebuilds.
#
# We hash the requirements file plus every .py file under the source
# directory (excluding tests/). Any change in any of those bumps the
# hash and triggers the null_resource to rebuild the package on next
# apply.
###############################################################################

locals {
  source_hash = sha256(join("|", concat(
    [filesha256(local.requirements_file_path)],
    [
      for f in local.source_py_files :
      filesha256("${var.source_dir}/${f}")
      if !startswith(f, "tests/")
    ],
  )))
}

###############################################################################
# Package builder — pip install + copy handler.py + seeder.py +
# mapping.py.
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

      # Copy the entry point + algorithm + mapper. We deliberately do
      # NOT copy tests/ or pyproject.toml — they belong only in the
      # source tree, never in the deployment image.
      cp "$SOURCE_DIR/handler.py" "$PACKAGE_DIR/handler.py"
      cp "$SOURCE_DIR/seeder.py"  "$PACKAGE_DIR/seeder.py"
      cp "$SOURCE_DIR/mapping.py" "$PACKAGE_DIR/mapping.py"

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
# Same pattern as migration-runner.
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

# S3 GetObject — scoped to ONE bucket + ONE key. The seeder is
# strictly read-only against S3.
resource "aws_iam_role_policy" "s3_get_object" {
  name = "${local.function_name}-s3-get-object"
  role = aws_iam_role.exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = "s3:GetObject"
        Resource = format(
          "arn:%s:s3:::%s/%s",
          data.aws_partition.current.partition,
          var.seed_s3_bucket,
          var.seed_s3_key,
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

  kms_key_arn = var.kms_key_arn

  environment {
    variables = {
      DB_HOST              = var.db_host
      DB_PORT              = tostring(var.db_port)
      DB_NAME              = var.db_name
      DB_USER              = var.db_user
      SEED_S3_BUCKET       = var.seed_s3_bucket
      SEED_S3_KEY          = var.seed_s3_key
      SEED_SAMPLE_FRACTION = tostring(var.seed_sample_fraction)
      LOG_LEVEL            = var.log_level
    }
  }

  tags = merge(local.common_tags, {
    Name = local.function_name
  })

  # Make sure the log group exists before the first invocation so
  # CloudWatch does not create a 'Never expire' group as a side
  # effect.
  depends_on = [
    aws_cloudwatch_log_group.this,
    aws_iam_role_policy_attachment.vpc,
    aws_iam_role_policy.rds_db_connect,
    aws_iam_role_policy.s3_get_object,
  ]
}

###############################################################################
# Synchronous invocation on every `terraform apply` (when triggered).
#
# The seeder is invoked AFTER the migration runner has applied the
# schema; the dev composition is responsible for sequencing this via
# explicit `depends_on` between the modules.
#
# Triggers are deliberately tied to BOTH:
#   * source_hash — anything that would change the deployment package
#     (handler.py, seeder.py, mapping.py, requirements.txt).
#   * function version — bumps when the Lambda is replaced for any
#     other reason (config change, IAM change, etc.).
#   * invocation_payload — operator-driven re-runs against a different
#     bucket/key/fraction.
#
# This means a `terraform apply` that does not touch the Lambda will
# NOT re-invoke the seeder. The seeder is itself idempotent (asset-
# first ON CONFLICT (storage_uri) DO NOTHING short-circuits the whole
# record on a re-run), so a stray re-invocation would be safe — but
# skipping it keeps applies fast in the common "no schema changes"
# case AND avoids the 5–15 minute wall-clock cost of a re-seed.
###############################################################################

resource "aws_lambda_invocation" "seed" {
  count = var.invoke_on_apply ? 1 : 0

  function_name = aws_lambda_function.this.function_name

  triggers = {
    source_hash        = local.source_hash
    function_version   = aws_lambda_function.this.version
    function_qualifier = aws_lambda_function.this.qualified_arn
    invocation_payload = var.invocation_payload
  }

  input = var.invocation_payload

  depends_on = [
    aws_lambda_function.this,
    aws_iam_role_policy.rds_db_connect,
    aws_iam_role_policy.s3_get_object,
    aws_cloudwatch_log_group.this,
  ]
}

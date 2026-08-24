###############################################################################
# Allen BioData Registry PoC — lambdas/migration-runner module
#
# Provisions:
#   * A Python 3.12 Lambda function packaged from
#     services/migration-runner/. The packaging step pip-installs the
#     runtime deps (currently just pg8000) into a build directory,
#     copies handler.py + runner.py alongside, copies every *.sql file
#     from the migrations/ directory into build/migrations/, and zips
#     the result.
#   * An IAM execution role with:
#       - AWSLambdaVPCAccessExecutionRole (ENI mgmt + CloudWatch Logs).
#       - A scoped inline policy granting `rds-db:connect` to ONE
#         specific {cluster_resource_id, db_user} tuple — Aurora's IAM
#         database authentication. No Secrets Manager grants, no
#         master-password path, by design (R31.4 spirit, R32.4).
#   * A CloudWatch Logs group with retention pinned via variable.
#   * VPC config so the Lambda can reach Aurora's private subnets.
#   * An `aws_lambda_invocation` data source that invokes the Lambda
#     synchronously on every `terraform apply`. The invocation runs
#     after the Lambda is provisioned and treats a non-2xx response
#     as a failed apply — exactly the behavior we want for a
#     bring-up-time migration runner.
#
# Validates: R32.5 (idempotent `terraform apply`).
# Design references:
#   * design.md §IaC.Idempotency and Sample Data.
#   * migrations/README.md (runner contract).
###############################################################################

locals {
  common_tags = merge(
    {
      Project     = var.project
      Environment = var.environment
      Module      = "lambdas/migration-runner"
      ManagedBy   = "terraform"
    },
    var.tags,
  )

  function_name = "${var.name_prefix}-migration-runner"

  # If the caller did not provide a build directory, stash the staging
  # tree under the calling Terraform working directory's `.terraform`
  # cache. Doing this per-module-instance keeps multiple compositions
  # from clobbering each other's builds.
  build_dir   = coalesce(var.build_dir, "${path.root}/.terraform/biodata-registry/${var.name_prefix}-migration-runner-build")
  package_dir = "${local.build_dir}/package"
  zip_path    = "${local.build_dir}/migration-runner.zip"

  # Source files we hash to decide when the package needs rebuilding.
  # We deliberately scan handler.py + runner.py + every migration file
  # — the migrations are part of the deployment image, so changing one
  # must trigger a rebuild.
  source_py_files        = fileset(var.source_dir, "**/*.py")
  migration_sql_files    = fileset(var.migrations_dir, "*.sql")
  requirements_file_path = "${var.source_dir}/requirements.txt"
}

data "aws_partition" "current" {}
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

###############################################################################
# Source-tree hash — drives package rebuilds.
#
# We hash the requirements file, every .py file under the source
# directory (excluding tests/), and every *.sql file in the migrations
# directory. Any change in any of those bumps the hash and triggers
# the null_resource to rebuild the package on next apply.
###############################################################################

locals {
  source_hash = sha256(join("|", concat(
    [filesha256(local.requirements_file_path)],
    [
      for f in local.source_py_files :
      filesha256("${var.source_dir}/${f}")
      if !startswith(f, "tests/")
    ],
    [
      for f in local.migration_sql_files :
      filesha256("${var.migrations_dir}/${f}")
    ],
  )))
}

###############################################################################
# Package builder — pip install + copy handler.py + runner.py +
# migrations/*.sql.
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
      SOURCE_DIR     = var.source_dir
      MIGRATIONS_DIR = var.migrations_dir
      PACKAGE_DIR    = local.package_dir
      PYTHON_BIN     = var.python_executable
    }
    command = <<-EOT
      set -euo pipefail

      # Wipe and recreate the staging dir so a previous failed build
      # cannot pollute the next zip with stale files.
      rm -rf "$PACKAGE_DIR"
      mkdir -p "$PACKAGE_DIR"
      mkdir -p "$PACKAGE_DIR/migrations"

      # Install runtime deps directly into the package root — Lambda
      # extracts the zip into its working directory, so deps need to
      # sit alongside handler.py.
      "$PYTHON_BIN" -m pip install \
        --quiet \
        --no-compile \
        --target "$PACKAGE_DIR" \
        --requirement "$SOURCE_DIR/requirements.txt"

      # Copy the entry point + algorithm. We deliberately do NOT copy
      # tests/ or pyproject.toml — they belong only in the source tree,
      # never in the deployment image.
      cp "$SOURCE_DIR/handler.py" "$PACKAGE_DIR/handler.py"
      cp "$SOURCE_DIR/runner.py"  "$PACKAGE_DIR/runner.py"

      # Copy the migrations corpus into the deployment image. The
      # Lambda reads from /var/task/migrations/ at runtime — this is
      # the directory that gets unpacked there.
      #
      # We use cp -p to preserve mtime so re-runs of the build don't
      # change file metadata unnecessarily, but we deliberately do
      # NOT copy README.md or anything other than *.sql (the runner's
      # filename pattern would reject those anyway, but excluding them
      # at packaging time is simpler than relying on the runner to
      # filter).
      for f in "$MIGRATIONS_DIR"/*.sql; do
        if [ -f "$f" ]; then
          cp -p "$f" "$PACKAGE_DIR/migrations/$(basename "$f")"
        fi
      done

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
      DB_HOST   = var.db_host
      DB_PORT   = tostring(var.db_port)
      DB_NAME   = var.db_name
      DB_USER   = var.db_user
      LOG_LEVEL = var.log_level
      # Raised from the 10s default so heavier data-reprojection migrations
      # (JSONB extraction over the full asset corpus) don't trip the pg8000
      # socket read timeout mid-statement.
      DB_CONNECT_TIMEOUT_SECONDS = "600"
      # MIGRATIONS_DIR intentionally not set — the handler defaults to
      # /var/task/migrations, which is exactly where our packaging step
      # placed the .sql files.
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

###############################################################################
# Synchronous invocation on every `terraform apply`.
#
# `aws_lambda_invocation` is a *data source* (technically: terraform-aws-
# provider exposes both a resource and a managed data variant; we use
# the managed resource so the invocation participates in Terraform's
# graph and triggers replays when its triggers change).
#
# Triggers are deliberately tied to BOTH:
#   * source_hash — anything that would change the deployment package
#     (handler.py, runner.py, requirements.txt, any migration file).
#   * function version — bumps when the Lambda is replaced for any
#     other reason (config change, IAM change, etc.).
#
# This means a `terraform apply` that does not touch the Lambda will
# NOT re-invoke the runner. The runner is itself idempotent, so a
# stray re-invocation would be safe — but skipping it keeps applies
# fast in the common "no schema changes" case.
###############################################################################

resource "aws_lambda_invocation" "migrate" {
  count = var.invoke_on_apply ? 1 : 0

  function_name = aws_lambda_function.this.function_name

  # Trigger replays whenever the package or function version changes.
  # The function's `last_modified` attribute changes any time the
  # function or its config is updated — exactly the signal we want.
  triggers = {
    source_hash        = local.source_hash
    function_version   = aws_lambda_function.this.version
    function_qualifier = aws_lambda_function.this.qualified_arn
    invocation_payload = var.invocation_payload
  }

  # The handler accepts an empty event by default; the variable lets
  # operators pass {"applied_by": "..."} or similar overrides without
  # editing the module.
  input = var.invocation_payload

  depends_on = [
    aws_lambda_function.this,
    aws_iam_role_policy.rds_db_connect,
    aws_cloudwatch_log_group.this,
  ]
}

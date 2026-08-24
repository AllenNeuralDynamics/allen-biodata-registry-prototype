###############################################################################
# Allen BioData Registry PoC — search Lambda module.
#
# Read-only Lambda fronting OpenSearch Serverless. Backs the GET /search
# and GET /suggest API Gateway routes.
#
# Source: services/search-lambda/handler.py
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
  function_name = "${var.name_prefix}-search"
  build_dir     = coalesce(var.build_dir, "${path.root}/.terraform/biodata-registry/${local.function_name}-build")
  package_dir   = "${local.build_dir}/package"
  zip_path      = "${local.build_dir}/handler.zip"
  source_hash   = sha1(join("", [for f in fileset(var.source_dir, "**/*") : filesha1("${var.source_dir}/${f}")]))

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
          --quiet \
          --no-compile \
          --platform manylinux2014_x86_64 \
          --only-binary=:all: \
          --python-version 3.12 \
          --target "$PACKAGE_DIR" \
          --requirement "$SOURCE_DIR/requirements.txt"
      fi

      cp "$SOURCE_DIR/handler.py" "$PACKAGE_DIR/handler.py"

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

resource "aws_iam_role_policy" "opensearch" {
  name = "${local.function_name}-opensearch"
  role = aws_iam_role.exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "aoss:APIAccessAll",
        "aoss:DashboardsAccessAll",
      ]
      Resource = var.opensearch_collection_arn
    }]
  })
}

# RDS IAM auth — only when Aurora env is configured (NL search enabled).
resource "aws_iam_role_policy" "rds_db_connect" {
  count = var.aurora_cluster_resource_id != "" ? 1 : 0

  name = "${local.function_name}-rds-db-connect"
  role = aws_iam_role.exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "rds-db:connect"
      Resource = "arn:aws:rds-db:${var.region}:${var.aws_account_id}:dbuser:${var.aurora_cluster_resource_id}/${var.aurora_db_user}"
    }]
  })
}

# Bedrock — KB retrieve_and_generate + invoke model. Only when KB is configured.
resource "aws_iam_role_policy" "bedrock_nl" {
  count = var.bedrock_kb_id != "" ? 1 : 0

  name = "${local.function_name}-bedrock-nl"
  role = aws_iam_role.exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "bedrock:Retrieve",
          "bedrock:RetrieveAndGenerate",
        ]
        Resource = "arn:aws:bedrock:${var.region}:${var.aws_account_id}:knowledge-base/${var.bedrock_kb_id}"
      },
      {
        Effect = "Allow"
        Action = ["bedrock:InvokeModel"]
        Resource = [
          "arn:aws:bedrock:${var.region}::foundation-model/*",
          "arn:aws:bedrock:${var.region}:${var.aws_account_id}:inference-profile/*",
          "arn:aws:bedrock:*::foundation-model/*",
        ]
      },
      {
        # Bedrock RetrieveAndGenerate validates the inference profile by
        # calling GetInferenceProfile internally — without this allow,
        # the call returns AccessDeniedException.
        Effect = "Allow"
        Action = [
          "bedrock:GetInferenceProfile",
          "bedrock:GetFoundationModel",
        ]
        Resource = [
          "arn:aws:bedrock:${var.region}:${var.aws_account_id}:inference-profile/*",
          "arn:aws:bedrock:*::foundation-model/*",
        ]
      },
    ]
  })
}

# Secrets Manager — Redis auth token (only when configured).
resource "aws_iam_role_policy" "redis_secret" {
  count = var.redis_auth_token_secret_arn != "" ? 1 : 0

  name = "${local.function_name}-redis-secret"
  role = aws_iam_role.exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = var.redis_auth_token_secret_arn
      },
      # Decrypt the secret with the customer-managed KMS key. We allow
      # any key in the account; tighter scoping requires the elasticache
      # module to expose the key ARN as a separate variable.
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = "*"
      },
    ]
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

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = var.security_group_ids
  }

  environment {
    variables = {
      OPENSEARCH_ENDPOINT     = var.opensearch_endpoint
      OPENSEARCH_REGION       = var.region
      LOG_LEVEL               = var.log_level

      # NL search (POST /search/nl) — env vars referenced by handler.py.
      # Empty values are tolerated — the NL path returns 503 when KB is
      # unset.
      BEDROCK_KB_ID              = var.bedrock_kb_id
      NL_MODEL_ID                = var.nl_model_id
      AWS_ACCOUNT_ID             = var.aws_account_id
      AURORA_HOST                = var.aurora_host
      AURORA_PORT                = tostring(var.aurora_port)
      AURORA_DB                  = var.aurora_db_name
      AURORA_DB_USER             = var.aurora_db_user
      AURORA_CLUSTER_RESOURCE_ID = var.aurora_cluster_resource_id
      REDIS_PRIMARY_ENDPOINT     = var.redis_primary_endpoint
      REDIS_AUTH_TOKEN_SECRET    = var.redis_auth_token_secret_arn
    }
  }

  tags = local.common_tags

  depends_on = [
    aws_cloudwatch_log_group.this,
    aws_iam_role_policy_attachment.vpc,
    aws_iam_role_policy.opensearch,
  ]
}

###############################################################################
# Allen BioData Registry PoC — mcp-server module.
#
# Deploys the external MCP server Lambda + the IAM grants it needs to
# invoke the read-only inner Lambdas (Search, Registration, Collections,
# Validation, Observability) and to query Aurora directly under RLS.
#
# The Lambda code is built by the shared `business` module; this module
# adds the extra IAM statements and environment variables on top.
#
# Validates: R16.1, R16.2, R16.3, R16.4, R16.5, R16.6 |
# Design: §External Interfaces.MCP Server (external),
# §IaC.Terraform Modules (`mcp-server`).
###############################################################################

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

module "lambda" {
  source = "../lambdas/business"

  name_prefix       = var.name_prefix
  function_suffix   = "mcp-server"
  environment       = var.environment
  project           = var.project
  region            = var.region
  python_executable = var.python_executable
  source_dir        = var.source_dir

  aurora_host                = var.aurora_host
  aurora_port                = var.aurora_port
  aurora_db_name             = var.aurora_db_name
  db_user                    = var.db_user
  aurora_cluster_resource_id = var.aurora_cluster_resource_id

  subnet_ids         = var.subnet_ids
  security_group_ids = var.security_group_ids

  memory_mb       = 1024
  timeout_seconds = 30

  extra_environment = {
    FN_SEARCH        = var.search_lambda_name
    FN_REGISTRATION  = var.registration_lambda_name
    FN_COLLECTIONS   = var.collections_lambda_name
    FN_VALIDATION    = var.validation_lambda_name
    FN_OBSERVABILITY = var.observability_lambda_name
  }

  extra_iam_statements = [
    # Invoke ONLY the read-only Lambdas. Writer Lambdas are intentionally
    # excluded so the MCP server inherits the read-only-agent invariant.
    {
      Effect = "Allow"
      Action = ["lambda:InvokeFunction"]
      Resource = [
        var.search_lambda_arn,
        var.registration_lambda_arn,
        var.collections_lambda_arn,
        var.validation_lambda_arn,
        var.observability_lambda_arn,
      ]
    },
  ]

  tags = var.tags
}

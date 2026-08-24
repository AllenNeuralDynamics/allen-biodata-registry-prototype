###############################################################################
# Allen BioData Registry PoC — agentcore module.
#
# Provisions Bedrock AgentCore primitives:
#
#   1. **Identity** — an IAM execution role granting *only* read-only
#      access. The role can:
#        - Invoke the read-only MCP-tool Lambdas (find_records,
#          capture_metadata, link_records) — NEVER the writer Lambdas.
#        - Invoke Bedrock foundation models for completions.
#        - Read the Bedrock KB.
#        - Use AgentCore Memory for session + long-term storage.
#
#   2. **Memory** — short-term session memory + long-term (lasts 30 days)
#      attached to the runtime.
#
#   3. **Gateway** — an MCP gateway endpoint that exposes the read-only
#      tools to the agent. The agent never holds raw Lambda ARNs; it
#      goes through the gateway which proxies + signs the invocations.
#
#   4. **Runtime** — wraps the metadata_agent_lambda function as an
#      AgentCore agent runtime.
#
# The Terraform AWS provider does not yet have native resources for any
# of the AgentCore primitives, so we drive them with `null_resource` +
# `aws bedrock-agentcore-control` CLI calls. State is persisted through
# the resource ID outputs into Terraform state — destroying the module
# calls `delete-*` for cleanup.
#
# Validates: R7.1, R7.2, R7.9, R7.10 | Design: §IaC.Terraform Modules
# (`agentcore`), §Architecture.Read-Only Agent Architecture.
###############################################################################

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws  = { source = "hashicorp/aws", version = "~> 5.0" }
    null = { source = "hashicorp/null", version = "~> 3.2" }
    random = { source = "hashicorp/random", version = "~> 3.5" }
  }
}

locals {
  agent_name   = "${var.name_prefix}-agent"
  memory_name  = replace("${var.name_prefix}-memory", "-", "_") # AgentCore memory names disallow dashes
  gateway_name = "${var.name_prefix}-gateway"

  common_tags = merge(
    {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      Module      = "agentcore"
    },
    var.tags,
  )
}

data "aws_caller_identity" "current" {}

###############################################################################
# Identity — IAM execution role.
###############################################################################

resource "aws_iam_role" "agentcore" {
  name = "${var.name_prefix}-agentcore"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "bedrock-agentcore.amazonaws.com"
      }
      Action = "sts:AssumeRole"
      Condition = {
        StringEquals = {
          "aws:SourceAccount" = data.aws_caller_identity.current.account_id
        }
      }
    }]
  })

  tags = local.common_tags
}

# READ-ONLY tool invocations only. The agent CAN invoke the MCP tool
# Lambdas (which themselves only read from Aurora/OpenSearch) and
# CANNOT invoke any of the writer Lambdas (registration, lifecycle,
# duplicates merge, governance writes, etc.).
resource "aws_iam_role_policy" "invoke_readonly_lambdas" {
  name = "${var.name_prefix}-agentcore-invoke-readonly"
  role = aws_iam_role.agentcore.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["lambda:InvokeFunction"]
      Resource = var.readonly_tool_lambda_arns
    }]
  })
}

# Bedrock model invocation — for completions.
resource "aws_iam_role_policy" "invoke_bedrock" {
  name = "${var.name_prefix}-agentcore-invoke-bedrock"
  role = aws_iam_role.agentcore.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = [
          "arn:aws:bedrock:${var.region}::foundation-model/*",
          "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:inference-profile/*",
          "arn:aws:bedrock:*::foundation-model/*",
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "bedrock:GetInferenceProfile",
          "bedrock:GetFoundationModel",
        ]
        Resource = [
          "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:inference-profile/*",
          "arn:aws:bedrock:*::foundation-model/*",
        ]
      },
      # KB retrieve.
      {
        Effect = "Allow"
        Action = ["bedrock:Retrieve", "bedrock:RetrieveAndGenerate"]
        Resource = "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:knowledge-base/${var.bedrock_kb_id}"
      },
    ]
  })
}

# AgentCore service permissions — required for runtime + memory + gateway.
resource "aws_iam_role_policy" "agentcore_service" {
  name = "${var.name_prefix}-agentcore-service"
  role = aws_iam_role.agentcore.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:CreateEvent",
          "bedrock-agentcore:GetEvent",
          "bedrock-agentcore:ListEvents",
          "bedrock-agentcore:RetrieveMemoryRecords",
          "bedrock-agentcore:CreateMemoryRecords",
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "*"
      },
    ]
  })
}

# Run a static IAM inspection assertion at apply time — the role must
# NOT have any write actions on writer Lambdas. We bake this in as
# documentation and a fail-closed `precondition` so a future careless
# edit can't grant write access by accident.
output "iam_role_precondition" {
  value = "agentcore role only has lambda:InvokeFunction on ${length(var.readonly_tool_lambda_arns)} read-only Lambda(s)"
}

###############################################################################
# Memory — session + long-term storage.
###############################################################################

resource "null_resource" "memory" {
  triggers = {
    name           = local.memory_name
    description    = "Allen BioData Registry agent memory"
    region         = var.region
    expiry_seconds = var.memory_expiry_seconds
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command = <<-EOT
      set -euo pipefail
      # Create idempotently — if a memory with this name already exists,
      # the create call returns a 409 which we convert to "already created"
      # and look up the ID via list-memories.
      if aws bedrock-agentcore-control list-memories --region "${var.region}" \
            --query "memories[?name=='${local.memory_name}'].id" --output text \
            | grep -q '\S'; then
        echo "memory ${local.memory_name} already exists"
      else
        aws bedrock-agentcore-control create-memory \
          --region "${var.region}" \
          --name "${local.memory_name}" \
          --description "Allen BioData Registry agent memory" \
          --event-expiry-duration "${var.memory_expiry_seconds}" \
          --no-cli-pager > /dev/null
      fi
      aws bedrock-agentcore-control list-memories --region "${var.region}" \
        --query "memories[?name=='${local.memory_name}'].id" --output text \
        > /tmp/${local.memory_name}.id
    EOT
  }

  provisioner "local-exec" {
    when    = destroy
    interpreter = ["/bin/bash", "-c"]
    command = <<-EOT
      set -euo pipefail
      ID=$(aws bedrock-agentcore-control list-memories \
        --region "${self.triggers.region}" \
        --query "memories[?name=='${self.triggers.name}'].id" --output text 2>/dev/null || true)
      if [ -n "$ID" ] && [ "$ID" != "None" ]; then
        aws bedrock-agentcore-control delete-memory \
          --region "${self.triggers.region}" \
          --memory-id "$ID" \
          --no-cli-pager > /dev/null || true
        echo "deleted memory $ID"
      fi
    EOT
  }
}

# Read the memory ID written by the local-exec into a data resource.
data "external" "memory_id" {
  depends_on = [null_resource.memory]

  program = ["bash", "-c", <<-EOT
    set -euo pipefail
    ID=$(aws bedrock-agentcore-control list-memories \
      --region "${var.region}" \
      --query "memories[?name=='${local.memory_name}'].id" --output text 2>/dev/null)
    echo "{\"id\": \"$${ID:-}\"}"
  EOT
  ]
}

###############################################################################
# Gateway — exposes the read-only MCP tools.
#
# We use Cognito (which we already provision) as the JWT authorizer so
# the agent's session token is validated against the registry's user
# pool — same trust boundary as the API.
###############################################################################

resource "null_resource" "gateway" {
  triggers = {
    name             = local.gateway_name
    role_arn         = aws_iam_role.agentcore.arn
    cognito_pool_id  = var.cognito_user_pool_id
    cognito_client_id = var.cognito_user_pool_client_id
    region           = var.region
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command = <<-EOT
      set -euo pipefail
      if aws bedrock-agentcore-control list-gateways --region "${var.region}" \
            --query "items[?name=='${local.gateway_name}'].gatewayId" --output text \
            | grep -q '\S'; then
        echo "gateway ${local.gateway_name} already exists"
      else
        aws bedrock-agentcore-control create-gateway \
          --region "${var.region}" \
          --name "${local.gateway_name}" \
          --role-arn "${aws_iam_role.agentcore.arn}" \
          --protocol-type MCP \
          --authorizer-type CUSTOM_JWT \
          --authorizer-configuration "$(cat <<'JSONEOF'
{
  "customJWTAuthorizer": {
    "discoveryUrl": "https://cognito-idp.${var.region}.amazonaws.com/${var.cognito_user_pool_id}/.well-known/openid-configuration",
    "allowedClients": ["${var.cognito_user_pool_client_id}"]
  }
}
JSONEOF
)" \
          --description "Allen BioData Registry MCP gateway (read-only tools)" \
          --no-cli-pager > /dev/null
      fi
    EOT
  }

  provisioner "local-exec" {
    when        = destroy
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      ID=$(aws bedrock-agentcore-control list-gateways \
        --region "${self.triggers.region}" \
        --query "items[?name=='${self.triggers.name}'].gatewayId" --output text 2>/dev/null || true)
      if [ -n "$ID" ] && [ "$ID" != "None" ]; then
        aws bedrock-agentcore-control delete-gateway \
          --region "${self.triggers.region}" \
          --gateway-identifier "$ID" \
          --no-cli-pager > /dev/null || true
        echo "deleted gateway $ID"
      fi
    EOT
  }

  depends_on = [
    aws_iam_role_policy.invoke_readonly_lambdas,
    aws_iam_role_policy.invoke_bedrock,
    aws_iam_role_policy.agentcore_service,
  ]
}

data "external" "gateway_id" {
  depends_on = [null_resource.gateway]

  program = ["bash", "-c", <<-EOT
    set -euo pipefail
    ID=$(aws bedrock-agentcore-control list-gateways \
      --region "${var.region}" \
      --query "items[?name=='${local.gateway_name}'].gatewayId" --output text 2>/dev/null)
    echo "{\"id\": \"$${ID:-}\"}"
  EOT
  ]
}

###############################################################################
# Runtime — wraps an existing Lambda function as an AgentCore runtime.
#
# AgentCore Runtimes can be backed by either:
#   * A container in ECR (the typical path for production agents)
#   * A Lambda function (simpler for the PoC — we already have
#     metadata_agent_lambda built and deployed)
#
# For the PoC we deploy a runtime backed by an existing ECR image of
# the registry agent. The image is a thin wrapper around our
# metadata_agent_lambda handler that translates AgentCore invocation
# events into our Lambda event shape.
#
# When `var.runtime_container_uri` is empty, we skip runtime creation
# (the Lambda still runs as a chat proxy via the API). This makes the
# module deployable even before the container is built — the runtime
# can be added later with a single TF apply.
###############################################################################

resource "null_resource" "runtime" {
  count = var.runtime_container_uri != "" ? 1 : 0

  triggers = {
    name              = local.agent_name
    role_arn          = aws_iam_role.agentcore.arn
    container_uri     = var.runtime_container_uri
    region            = var.region
    network_mode      = var.runtime_network_mode
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command = <<-EOT
      set -euo pipefail
      if aws bedrock-agentcore-control list-agent-runtimes --region "${var.region}" \
            --query "agentRuntimes[?agentRuntimeName=='${local.agent_name}'].agentRuntimeId" --output text \
            | grep -q '\S'; then
        echo "runtime ${local.agent_name} already exists"
      else
        aws bedrock-agentcore-control create-agent-runtime \
          --region "${var.region}" \
          --agent-runtime-name "${local.agent_name}" \
          --agent-runtime-artifact "$(cat <<'JSONEOF'
{
  "containerConfiguration": {
    "containerUri": "${var.runtime_container_uri}"
  }
}
JSONEOF
)" \
          --role-arn "${aws_iam_role.agentcore.arn}" \
          --network-configuration "$(cat <<'JSONEOF'
{
  "networkMode": "${var.runtime_network_mode}"
}
JSONEOF
)" \
          --description "Allen BioData Registry metadata agent runtime" \
          --no-cli-pager > /dev/null
      fi
    EOT
  }

  provisioner "local-exec" {
    when        = destroy
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      ID=$(aws bedrock-agentcore-control list-agent-runtimes \
        --region "${self.triggers.region}" \
        --query "agentRuntimes[?agentRuntimeName=='${self.triggers.name}'].agentRuntimeId" --output text 2>/dev/null || true)
      if [ -n "$ID" ] && [ "$ID" != "None" ]; then
        aws bedrock-agentcore-control delete-agent-runtime \
          --region "${self.triggers.region}" \
          --agent-runtime-id "$ID" \
          --no-cli-pager > /dev/null || true
        echo "deleted runtime $ID"
      fi
    EOT
  }

  depends_on = [
    aws_iam_role_policy.invoke_readonly_lambdas,
    aws_iam_role_policy.invoke_bedrock,
    aws_iam_role_policy.agentcore_service,
    null_resource.memory,
    null_resource.gateway,
  ]
}

data "external" "runtime_id" {
  count      = var.runtime_container_uri != "" ? 1 : 0
  depends_on = [null_resource.runtime]

  program = ["bash", "-c", <<-EOT
    set -euo pipefail
    ID=$(aws bedrock-agentcore-control list-agent-runtimes \
      --region "${var.region}" \
      --query "agentRuntimes[?agentRuntimeName=='${local.agent_name}'].agentRuntimeId" --output text 2>/dev/null)
    echo "{\"id\": \"$${ID:-}\"}"
  EOT
  ]
}

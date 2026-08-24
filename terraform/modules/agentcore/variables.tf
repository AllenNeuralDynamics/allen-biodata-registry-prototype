variable "name_prefix" {
  type    = string
  default = "biodata-registry-dev"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "project" {
  type    = string
  default = "biodata-registry"
}

variable "region" {
  type    = string
  default = "us-west-2"
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "readonly_tool_lambda_arns" {
  description = "ARNs of the read-only MCP tool Lambdas (find_records, capture_metadata, link_records). Writer Lambdas MUST NOT be in this list — that's the read-only-agent invariant."
  type        = list(string)
}

variable "bedrock_kb_id" {
  description = "ID of the Bedrock Knowledge Base the agent can retrieve from."
  type        = string
}

variable "cognito_user_pool_id" {
  description = "Cognito User Pool ID for JWT authorization on the gateway."
  type        = string
}

variable "cognito_user_pool_client_id" {
  description = "Cognito client ID allowed to authenticate to the gateway."
  type        = string
}

# Memory
variable "memory_expiry_seconds" {
  description = "Long-term memory event expiry, in days (CLI argument is a number of days). AgentCore minimum is 7, maximum 365. PoC uses 30."
  type        = number
  default     = 30
}

# Runtime
variable "runtime_container_uri" {
  description = "ECR image URI for the agent runtime container. Leave empty to defer runtime creation until the image is built."
  type        = string
  default     = ""
}

variable "runtime_network_mode" {
  description = "AgentCore runtime network mode. PUBLIC routes through the public internet; VPC pins the runtime into the registry's VPC (preferred for production)."
  type        = string
  default     = "PUBLIC"
  validation {
    condition     = contains(["PUBLIC", "VPC"], var.runtime_network_mode)
    error_message = "runtime_network_mode must be one of: PUBLIC, VPC"
  }
}

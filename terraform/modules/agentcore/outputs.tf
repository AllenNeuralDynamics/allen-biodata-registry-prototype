output "iam_role_arn" {
  description = "ARN of the AgentCore execution role."
  value       = aws_iam_role.agentcore.arn
}

output "memory_id" {
  description = "ID of the AgentCore Memory resource."
  value       = data.external.memory_id.result["id"]
}

output "gateway_id" {
  description = "ID of the AgentCore Gateway."
  value       = data.external.gateway_id.result["id"]
}

output "runtime_id" {
  description = "ID of the AgentCore Runtime, when configured."
  value       = var.runtime_container_uri != "" ? data.external.runtime_id[0].result["id"] : ""
}

###############################################################################
# Outputs — vpc module
#
# These outputs are the public contract consumed by every downstream
# data-plane module (aurora, documentdb, opensearch, elasticache, lambdas,
# cdc-pipeline, ...). Renaming or removing any of them is a breaking change.
###############################################################################

output "vpc_id" {
  description = "ID of the VPC."
  value       = aws_vpc.this.id
}

output "vpc_cidr" {
  description = "Primary CIDR block of the VPC."
  value       = aws_vpc.this.cidr_block
}

output "vpc_arn" {
  description = "ARN of the VPC."
  value       = aws_vpc.this.arn
}

output "private_subnet_ids" {
  description = "IDs of the private subnets, in the same order as var.availability_zones. Consumed by Aurora, DocumentDB, OpenSearch, ElastiCache, and Lambda VPC config."
  value       = aws_subnet.private[*].id
}

output "private_subnet_cidrs" {
  description = "CIDR blocks of the private subnets."
  value       = aws_subnet.private[*].cidr_block
}

output "public_subnet_ids" {
  description = "IDs of the public subnets, in the same order as var.availability_zones. Currently only used by NAT gateways; reserve for future ALB/NLB needs."
  value       = aws_subnet.public[*].id
}

output "public_subnet_cidrs" {
  description = "CIDR blocks of the public subnets."
  value       = aws_subnet.public[*].cidr_block
}

output "availability_zones" {
  description = "Availability zones in use, mirrored from input for downstream convenience."
  value       = var.availability_zones
}

output "internet_gateway_id" {
  description = "ID of the Internet Gateway."
  value       = aws_internet_gateway.this.id
}

output "nat_gateway_ids" {
  description = "IDs of the NAT gateway(s). Length 1 in PoC mode (single_nat_gateway = true), length = AZ count in HA mode."
  value       = aws_nat_gateway.this[*].id
}

output "private_route_table_ids" {
  description = "IDs of the per-AZ private route tables."
  value       = aws_route_table.private[*].id
}

output "public_route_table_id" {
  description = "ID of the shared public route table."
  value       = aws_route_table.public.id
}

output "internal_security_group_id" {
  description = "ID of the baseline 'internal' security group. Attach this SG to every VPC-bound data-plane resource (Aurora, DocumentDB, OpenSearch, ElastiCache, Lambda) to grant intra-VPC connectivity."
  value       = aws_security_group.internal.id
}

output "endpoints_security_group_id" {
  description = "ID of the SG attached to the interface VPC endpoints. Exported for diagnostic / advanced module use; downstream modules typically don't need this directly."
  value       = aws_security_group.endpoints.id
}

output "vpc_endpoint_s3_id" {
  description = "ID of the S3 gateway VPC endpoint."
  value       = aws_vpc_endpoint.s3.id
}

output "vpc_endpoint_dynamodb_id" {
  description = "ID of the DynamoDB gateway VPC endpoint."
  value       = aws_vpc_endpoint.dynamodb.id
}

output "interface_vpc_endpoint_ids" {
  description = "Map of interface VPC endpoint service names → endpoint IDs (Bedrock Runtime, Bedrock Agent Runtime, Cognito IDP, Secrets Manager, KMS — depending on enable_* toggles)."
  value       = { for k, v in aws_vpc_endpoint.interface : k => v.id }
}

output "interface_vpc_endpoint_dns" {
  description = "Map of interface VPC endpoint service names → DNS entries. Useful for debugging private DNS resolution."
  value = {
    for k, v in aws_vpc_endpoint.interface :
    k => [for entry in v.dns_entry : entry.dns_name]
  }
}

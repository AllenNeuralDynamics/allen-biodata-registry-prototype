###############################################################################
# Allen BioData Registry PoC — vpc module
#
# Provisions:
#   * Multi-AZ VPC (default 10.40.0.0/16, 3 AZs in us-west-2)
#   * 3 private /20 subnets (data plane: Aurora, DocumentDB, OpenSearch,
#     ElastiCache, Lambdas)
#   * 3 public /24 subnets (NAT gateways)
#   * Internet Gateway + single NAT Gateway (PoC cost optimization)
#   * Gateway VPC endpoints for S3 and DynamoDB (free)
#   * Interface VPC endpoints for Bedrock Runtime, Bedrock Agent Runtime,
#     Cognito IDP, Secrets Manager, KMS
#   * "internal" security group (intra-VPC traffic) and "endpoints" security
#     group (HTTPS from VPC CIDR) consumed by every downstream data-plane
#     module.
#
# Validates: R31.2 (VPC endpoints for Bedrock, S3, Cognito, DocumentDB),
# R32.2 (terraform apply provisions VPC + endpoints).
#
# Note on DocumentDB: DocumentDB does not expose a PrivateLink interface
# endpoint — it is reached via VPC routing using the cluster's network
# interfaces in private subnets. This module exports the private subnet IDs
# and the internal security group so the documentdb module (Task 4.1) can
# attach the cluster to those subnets and inherit network-level isolation
# (R31.2, R31.3, design.md §IaC.Terraform Modules `documentdb`).
###############################################################################

locals {
  common_tags = merge(
    {
      Project     = var.project
      Environment = var.environment
      Module      = "vpc"
      ManagedBy   = "terraform"
    },
    var.tags,
  )

  az_count = length(var.availability_zones)

  # /20 private subnets carved from the /16:
  # 10.40.0.0/20, 10.40.16.0/20, 10.40.32.0/20, ...
  private_subnet_cidrs = [
    for idx in range(local.az_count) :
    cidrsubnet(var.vpc_cidr, 4, idx)
  ]

  # /24 public subnets carved from the upper half of the /16:
  # 10.40.240.0/24, 10.40.241.0/24, 10.40.242.0/24, ...
  # Offset 240 keeps public well-separated from private and leaves headroom.
  public_subnet_cidrs = [
    for idx in range(local.az_count) :
    cidrsubnet(var.vpc_cidr, 8, 240 + idx)
  ]

  # Number of NAT Gateways: 1 for the PoC (cost optimization), one-per-AZ for HA.
  nat_gateway_count = var.single_nat_gateway ? 1 : local.az_count

  # Interface endpoints to provision. The Bedrock Agent Runtime endpoint is
  # toggleable because PrivateLink for bedrock-agent-runtime may not be GA in
  # every region — see variable description.
  interface_endpoint_services = concat(
    [
      "com.amazonaws.${data.aws_region.current.name}.cognito-idp",
      "com.amazonaws.${data.aws_region.current.name}.secretsmanager",
      "com.amazonaws.${data.aws_region.current.name}.kms",
    ],
    var.enable_bedrock_endpoints ? [
      "com.amazonaws.${data.aws_region.current.name}.bedrock-runtime",
    ] : [],
    var.enable_bedrock_endpoints && var.enable_bedrock_agent_runtime_endpoint ? [
      "com.amazonaws.${data.aws_region.current.name}.bedrock-agent-runtime",
    ] : [],
  )
}

data "aws_region" "current" {}

###############################################################################
# VPC
###############################################################################

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true # required for interface VPC endpoints to resolve

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-vpc"
  })
}

###############################################################################
# Internet Gateway (public egress)
###############################################################################

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-igw"
  })
}

###############################################################################
# Subnets
###############################################################################

resource "aws_subnet" "private" {
  count = local.az_count

  vpc_id                  = aws_vpc.this.id
  cidr_block              = local.private_subnet_cidrs[count.index]
  availability_zone       = var.availability_zones[count.index]
  map_public_ip_on_launch = false

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-private-${var.availability_zones[count.index]}"
    Tier = "private"
  })
}

resource "aws_subnet" "public" {
  count = local.az_count

  vpc_id                  = aws_vpc.this.id
  cidr_block              = local.public_subnet_cidrs[count.index]
  availability_zone       = var.availability_zones[count.index]
  map_public_ip_on_launch = false # explicit; NAT does not need public IPs on subnet defaults

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-public-${var.availability_zones[count.index]}"
    Tier = "public"
  })
}

###############################################################################
# NAT Gateway(s)
#
# For the PoC we provision ONE NAT in the first AZ (cost: ~$32/mo + data
# processing). All private subnets route through it. Production should set
# single_nat_gateway = false to provision one NAT per AZ for HA.
###############################################################################

resource "aws_eip" "nat" {
  count = local.nat_gateway_count

  domain = "vpc"

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-nat-eip-${count.index}"
  })

  depends_on = [aws_internet_gateway.this]
}

resource "aws_nat_gateway" "this" {
  count = local.nat_gateway_count

  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-nat-${var.availability_zones[count.index]}"
  })

  depends_on = [aws_internet_gateway.this]
}

###############################################################################
# Route tables
###############################################################################

# Public route table — one shared across all public subnets, default route to IGW.
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-rt-public"
    Tier = "public"
  })
}

resource "aws_route" "public_internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  count = local.az_count

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# Private route tables — one per AZ so each AZ can route to its own NAT when
# single_nat_gateway = false; in PoC mode all three point at NAT[0].
resource "aws_route_table" "private" {
  count = local.az_count

  vpc_id = aws_vpc.this.id

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-rt-private-${var.availability_zones[count.index]}"
    Tier = "private"
  })
}

resource "aws_route" "private_nat" {
  count = local.az_count

  route_table_id         = aws_route_table.private[count.index].id
  destination_cidr_block = "0.0.0.0/0"
  # If single NAT, every private RT points at NAT[0]; otherwise to the AZ-local NAT.
  nat_gateway_id = var.single_nat_gateway ? aws_nat_gateway.this[0].id : aws_nat_gateway.this[count.index].id
}

resource "aws_route_table_association" "private" {
  count = local.az_count

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

###############################################################################
# Security groups
#
# - "internal": baseline SG used by every VPC-attached resource (Aurora,
#   DocumentDB, OpenSearch, ElastiCache, Lambda). Allows all traffic between
#   members of this SG and full egress.
# - "endpoints": dedicated SG for the interface VPC endpoints. Allows HTTPS
#   (443) from the VPC CIDR.
###############################################################################

resource "aws_security_group" "internal" {
  name        = "${var.name_prefix}-internal-sg"
  description = "Baseline intra-VPC SG. Members can talk to each other on all ports; full egress to the internet via NAT and to VPC endpoints."
  vpc_id      = aws_vpc.this.id

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-internal-sg"
  })
}

# Self-referencing ingress: members of the SG can reach each other on all ports.
resource "aws_vpc_security_group_ingress_rule" "internal_self" {
  security_group_id            = aws_security_group.internal.id
  referenced_security_group_id = aws_security_group.internal.id
  ip_protocol                  = "-1"
  description                  = "Allow all traffic between members of internal SG"
}

resource "aws_vpc_security_group_egress_rule" "internal_all" {
  security_group_id = aws_security_group.internal.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
  description       = "Allow all egress (NAT for internet, VPC routing for endpoints)"
}

resource "aws_security_group" "endpoints" {
  name        = "${var.name_prefix}-endpoints-sg"
  description = "Interface VPC endpoint SG. Allows HTTPS from VPC CIDR."
  vpc_id      = aws_vpc.this.id

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-endpoints-sg"
  })
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_https" {
  security_group_id = aws_security_group.endpoints.id
  cidr_ipv4         = aws_vpc.this.cidr_block
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  description       = "HTTPS from VPC CIDR to interface endpoints"
}

resource "aws_vpc_security_group_egress_rule" "endpoints_all" {
  security_group_id = aws_security_group.endpoints.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
  description       = "Egress (endpoint ENIs reply on ephemeral ports)"
}

###############################################################################
# VPC endpoints — gateway type (free)
#
# S3 and DynamoDB use gateway endpoints attached to the private route tables.
# They cost nothing and avoid NAT data-processing charges for S3/DDB traffic.
###############################################################################

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${data.aws_region.current.name}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = aws_route_table.private[*].id

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-vpce-s3"
  })
}

resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${data.aws_region.current.name}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = aws_route_table.private[*].id

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-vpce-dynamodb"
  })
}

###############################################################################
# VPC endpoints — interface type (paid, ~$7/mo each + data processing)
#
# - Bedrock Runtime + Bedrock Agent Runtime: required so the AI layer
#   (AgentCore, NL→SQL, embedding backfill) reaches Bedrock without traversing
#   the NAT. (R31.2)
# - Cognito IDP: lets the Authorizer Lambda validate JWTs without leaving the
#   VPC. (R31.2)
# - Secrets Manager: every Lambda fetches Aurora/DocumentDB credentials
#   here. (R31.1)
# - KMS: every encrypted resource (Aurora, DocumentDB, OpenSearch, Redis, S3)
#   needs Decrypt calls. (R31.3)
###############################################################################

resource "aws_vpc_endpoint" "interface" {
  for_each = toset(local.interface_endpoint_services)

  vpc_id              = aws_vpc.this.id
  service_name        = each.value
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-vpce-${replace(each.value, "com.amazonaws.${data.aws_region.current.name}.", "")}"
  })
}

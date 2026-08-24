###############################################################################
# Variables — vpc module
#
# Defaults are tuned for the Allen BioData Registry PoC (us-west-2, dev env).
# Every value can be overridden by the consuming environment composition.
###############################################################################

variable "name_prefix" {
  description = "Prefix applied to every resource name. Typically '<project>-<environment>', e.g. 'biodata-registry-dev'."
  type        = string
  default     = "biodata-registry-dev"

  validation {
    condition     = length(var.name_prefix) > 0 && length(var.name_prefix) <= 40
    error_message = "name_prefix must be 1–40 characters."
  }
}

variable "environment" {
  description = "Environment tag applied to every resource (dev, staging, prod)."
  type        = string
  default     = "dev"
}

variable "project" {
  description = "Project tag applied to every resource."
  type        = string
  default     = "biodata-registry"
}

variable "vpc_cidr" {
  description = "Primary IPv4 CIDR block for the VPC. Default 10.40.0.0/16 leaves room for 3 private /20 subnets and 3 public /24 subnets."
  type        = string
  default     = "10.40.0.0/16"

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0))
    error_message = "vpc_cidr must be a valid IPv4 CIDR block."
  }
}

variable "availability_zones" {
  description = "Availability zones to span. The module creates one private + one public subnet per AZ."
  type        = list(string)
  default     = ["us-west-2a", "us-west-2b", "us-west-2c"]

  validation {
    condition     = length(var.availability_zones) >= 2 && length(var.availability_zones) <= 6
    error_message = "Provide between 2 and 6 availability zones."
  }
}

variable "single_nat_gateway" {
  description = "If true (PoC default), provision exactly one NAT gateway in the first AZ to minimize cost (~$32/mo vs ~$96/mo for 3 NATs). Production environments should set this to false for HA."
  type        = bool
  default     = true
}

variable "enable_bedrock_endpoints" {
  description = "Provision interface endpoints for Bedrock Runtime and Bedrock Agent Runtime so VPC-bound Lambdas can reach Bedrock without traversing the NAT. Required by R31.2."
  type        = bool
  default     = true
}

variable "enable_bedrock_agent_runtime_endpoint" {
  description = "Provision the Bedrock Agent Runtime interface endpoint. Set to false if PrivateLink for bedrock-agent-runtime is not yet GA in the target region; the module will then fall back to NAT routing for AgentCore traffic. See R31.2 — TODO revisit when GA."
  type        = bool
  default     = true
}

variable "tags" {
  description = "Additional tags merged onto every resource. Project / Environment are added automatically."
  type        = map(string)
  default     = {}
}

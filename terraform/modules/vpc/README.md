# `vpc` Terraform module — Allen BioData Registry PoC

Provisions the network substrate for the Allen BioData Registry. Every
data-plane module (`aurora`, `documentdb`, `opensearch`, `elasticache`,
`lambdas`, `cdc-pipeline`) consumes its outputs.

**Validates:** R31.2 (VPC endpoints for Bedrock, S3, Cognito, DocumentDB),
R32.2 (Terraform provisions VPC + endpoints).

**Design reference:** `design.md` §Infrastructure as Code.Terraform Modules
(`vpc`).

---

## Layout

| | |
|---|---|
| **Default CIDR** | `10.40.0.0/16` |
| **AZs** | `us-west-2a`, `us-west-2b`, `us-west-2c` |
| **Private subnets** | three `/20` (e.g. `10.40.0.0/20`, `10.40.16.0/20`, `10.40.32.0/20`) — Aurora, DocumentDB, OpenSearch, ElastiCache, Lambdas |
| **Public subnets** | three `/24` (`10.40.240.0/24`, `10.40.241.0/24`, `10.40.242.0/24`) — NAT gateway only |
| **NAT gateways** | **one** in `us-west-2a` by default (PoC cost trade-off, see below) |
| **Internet Gateway** | one |
| **Route tables** | one shared public RT (default route → IGW); one private RT per AZ (default route → NAT) |

### Single-AZ NAT trade-off

The PoC defaults to **one** NAT gateway in the first AZ. Cost: roughly
**$32/mo + $0.045/GB** processed.

* **Pro:** ~3× cheaper than one-NAT-per-AZ (~$96/mo for 3 NATs), which is
  meaningful for a PoC that runs for a few weeks.
* **Con:** if the AZ hosting the NAT goes down, every private subnet loses
  egress. Lambdas in the surviving AZs cannot reach Bedrock through NAT (they
  *can* still reach Bedrock through the interface endpoint), Cognito (same),
  Secrets Manager (same), KMS (same) — so most of the registry survives — but
  any traffic that requires NAT (general Internet, third-party APIs) is down.

For production, set `single_nat_gateway = false` to provision one NAT per AZ.

---

## VPC endpoints

R31.2 requires VPC endpoints for Bedrock, S3, Cognito, and DocumentDB. The
module fulfills the requirement as follows:

| Service | Type | Notes |
|---|---|---|
| **S3** | Gateway | Free. Attached to all private route tables. |
| **DynamoDB** | Gateway | Free. Bonus over R31.2 — used by the Terraform state lock table. |
| **Bedrock Runtime** | Interface | ~$7.30/mo + per-GB. Toggle: `enable_bedrock_endpoints`. |
| **Bedrock Agent Runtime** | Interface | ~$7.30/mo + per-GB. Toggle: `enable_bedrock_agent_runtime_endpoint`. **TODO:** PrivateLink for `bedrock-agent-runtime` may not be GA in every region — set this to `false` to skip just this endpoint and fall back to NAT routing for AgentCore traffic. (R31.2.) |
| **Cognito IDP** | Interface | ~$7.30/mo. The Authorizer Lambda hits Cognito on every request. |
| **Secrets Manager** | Interface | ~$7.30/mo. Every Lambda fetches Aurora/DocDB credentials from here. |
| **KMS** | Interface | ~$7.30/mo. Decrypt calls for every encrypted resource. |
| **DocumentDB** | _none_ | DocumentDB does **not** expose a PrivateLink interface endpoint. It is reached via VPC routing using the cluster's network interfaces, which are launched in the **private subnets exported by this module**. The `documentdb` module (Task 4.1) attaches the cluster to `private_subnet_ids` and the `internal_security_group_id`, which is sufficient to satisfy R31.2's intent that DocumentDB traffic stays inside the VPC. |

Total interface-endpoint cost in PoC mode: **~$36/mo** (5 endpoints × ~$7.30) +
NAT (~$32/mo) = **~$68/mo** baseline before any data processing or
service-specific charges.

---

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| `name_prefix` | `string` | `"biodata-registry-dev"` | Prefix for every resource Name tag. |
| `environment` | `string` | `"dev"` | Environment tag. |
| `project` | `string` | `"biodata-registry"` | Project tag. |
| `vpc_cidr` | `string` | `"10.40.0.0/16"` | Primary IPv4 CIDR. |
| `availability_zones` | `list(string)` | `["us-west-2a", "us-west-2b", "us-west-2c"]` | AZs to span. |
| `single_nat_gateway` | `bool` | `true` | One NAT for the whole VPC (PoC) vs. one per AZ (HA). |
| `enable_bedrock_endpoints` | `bool` | `true` | Provision Bedrock Runtime endpoint. |
| `enable_bedrock_agent_runtime_endpoint` | `bool` | `true` | Provision Bedrock Agent Runtime endpoint. Set `false` if PrivateLink not GA. |
| `tags` | `map(string)` | `{}` | Extra tags merged onto every resource. |

## Outputs

| Name | Description |
|---|---|
| `vpc_id` | VPC ID. |
| `vpc_cidr` | VPC CIDR block. |
| `vpc_arn` | VPC ARN. |
| `private_subnet_ids` | List of private subnet IDs (AZ-ordered). |
| `private_subnet_cidrs` | CIDRs of the private subnets. |
| `public_subnet_ids` | List of public subnet IDs (AZ-ordered). |
| `public_subnet_cidrs` | CIDRs of the public subnets. |
| `availability_zones` | Mirrored input for downstream convenience. |
| `internet_gateway_id` | IGW ID. |
| `nat_gateway_ids` | List of NAT gateway IDs (length 1 in PoC mode). |
| `private_route_table_ids` | Per-AZ private route table IDs. |
| `public_route_table_id` | Shared public route table ID. |
| `internal_security_group_id` | Baseline SG. **Attach this to every data-plane resource.** |
| `endpoints_security_group_id` | SG attached to the interface VPC endpoints. |
| `vpc_endpoint_s3_id` | S3 gateway endpoint ID. |
| `vpc_endpoint_dynamodb_id` | DynamoDB gateway endpoint ID. |
| `interface_vpc_endpoint_ids` | `map(service_name → endpoint_id)` for the interface endpoints. |
| `interface_vpc_endpoint_dns` | `map(service_name → list(dns_name))` for debugging private DNS. |

---

## Example usage

In `terraform/envs/dev/main.tf`:

```hcl
provider "aws" {
  region = "us-west-2"

  default_tags {
    tags = {
      Project     = "biodata-registry"
      Environment = "dev"
      ManagedBy   = "terraform"
    }
  }
}

module "vpc" {
  source = "../../modules/vpc"

  name_prefix        = "biodata-registry-dev"
  environment        = "dev"
  project            = "biodata-registry"
  vpc_cidr           = "10.40.0.0/16"
  availability_zones = ["us-west-2a", "us-west-2b", "us-west-2c"]
  single_nat_gateway = true # PoC cost optimization

  tags = {
    Owner = "biodata-registry-team"
  }
}

module "aurora" {
  source = "../../modules/aurora"

  vpc_id             = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids
  security_group_ids = [module.vpc.internal_security_group_id]
  # ...
}

module "documentdb" {
  source = "../../modules/documentdb"

  vpc_id             = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids
  security_group_ids = [module.vpc.internal_security_group_id]
  # ...
}
```

> **Tip on `default_tags`:** declare `Project` / `Environment` once on the AWS
> provider and the module will still merge its own `Module = "vpc"` and any
> caller-supplied `tags` on top, so resource-level tags remain explicit.

---

## Validation

This module is consumed by the dev environment composition (`terraform/envs/dev`)
and is not deployed standalone. To verify the module compiles:

```bash
cd customers/NPO/RSC/Allen_Institute/biodata-registry/terraform/modules/vpc
terraform init -backend=false
terraform validate
terraform fmt -check
```

`terraform plan` / `apply` are run in Task 10 against the dev environment
composition, not against this module directly.

---

## TODOs handed to Task 10 (dev environment composition)

* Wire `module.vpc` into `terraform/envs/dev/main.tf` and pass the outputs to
  the `aurora`, `documentdb`, `opensearch`, `elasticache`, `lambdas`, and
  `cdc-pipeline` modules as those are authored in Tasks 3 / 4 / 12 / 17.
* If the customer's AWS account has a different VPC numbering scheme, override
  `vpc_cidr` and `availability_zones` in the dev composition.
* If PrivateLink for `bedrock-agent-runtime` is not GA in `us-west-2` at
  deploy time, set `enable_bedrock_agent_runtime_endpoint = false` in the dev
  composition until it becomes GA.
* Cost note for the customer review at QC1: VPC baseline is ~$68/mo
  (NAT + interface endpoints) before data charges. Production HA mode
  (`single_nat_gateway = false`) raises that to ~$132/mo.

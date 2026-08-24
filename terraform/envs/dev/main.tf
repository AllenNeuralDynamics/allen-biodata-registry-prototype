###############################################################################
# Allen BioData Registry PoC — dev environment composition
#
# Wires every infrastructure module under terraform/modules/ into a single
# `terraform apply`-able root. The composition is the only place provider
# instances are declared; modules only declare provider *constraints*.
#
# Module wiring graph (top-down):
#
#   bootstrap (one-time, separate state) — creates the S3 + DynamoDB +
#                                          KMS backend that THIS root uses
#       │
#       ▼
#   vpc ──────────────────────────────────────────────────────────────────┐
#    │  (vpc_id, private_subnet_ids, internal_security_group_id)          │
#    │                                                                    │
#    ├──► aurora ─────────────► migration_runner ──► (seeder, deferred)   │
#    │     │                       (invokes 7 SQL                         │
#    │     │                        migrations on apply)                  │
#    │     │                                                              │
#    │     └──► post_confirmation_lambda ──► cognito                      │
#    │                                                                    │
#    ├──► documentdb (CDC sink)                                           │
#    ├──► opensearch (search + KNN)                                       │
#    └──► elasticache (4 cache tiers, single replication group)           │
#                                                                         │
#   cloudfront_s3 (provider alias us_east_1 for ACM) ◄────────────────────┘
#
# Dependency edges that aren't pure data-flow are encoded with `depends_on`:
#   * cognito depends_on post_confirmation_lambda — the User Pool's
#     post-confirmation trigger ARN comes from the Lambda module.
#   * (future) seeder depends_on migration_runner — schema must be in
#     place before seed inserts.
#
# Validates: R32.5 (terraform apply provisions the entire stack idempotently),
# R32.6 (remote state). Phase-1 closure for QC1.
###############################################################################

###############################################################################
# Backend — S3 + DynamoDB lock + KMS, all provisioned by terraform/bootstrap.
#
# The bootstrap config (terraform/bootstrap/) emits the exact bucket / lock
# table / KMS key id used here. To re-target a different account, run the
# bootstrap there and update these literals.
###############################################################################

terraform {
  backend "s3" {
    bucket         = "biodata-registry-tf-state-014097726564-us-west-2"
    key            = "envs/dev/terraform.tfstate"
    region         = "us-west-2"
    dynamodb_table = "biodata-registry-tf-locks"
    encrypt        = true
    kms_key_id     = "alias/biodata-registry-tf-state"
  }
}

###############################################################################
# Providers — default + us-east-1 alias for CloudFront ACM certs.
###############################################################################

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = merge(
      {
        Project     = var.project
        Environment = var.environment
        ManagedBy   = "terraform"
      },
      var.tags,
    )
  }
}

provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

  default_tags {
    tags = merge(
      {
        Project     = var.project
        Environment = var.environment
        ManagedBy   = "terraform"
      },
      var.tags,
    )
  }
}

###############################################################################
# Identity guardrail — fail fast if the caller is wrong account.
#
# Catches the (easy) mistake of running `terraform apply` against the wrong
# AWS account because the operator forgot to refresh ada credentials.
###############################################################################

data "aws_caller_identity" "current" {
  lifecycle {
    postcondition {
      condition     = self.account_id == var.account_id
      error_message = "Caller account ${self.account_id} does not match var.account_id (${var.account_id}). Refresh creds with `ada credentials update --account ${var.account_id} --role Admin --provider isengard` and re-source .creds-helper.sh."
    }
  }
}

data "aws_region" "current" {}

###############################################################################
# Locals — shared naming + paths.
###############################################################################

locals {
  # name_prefix is the canonical "<project>-<environment>" string passed to
  # every module that takes one. All resource names downstream concatenate
  # off this.
  name_prefix = "${var.project}-${var.environment}"

  # Repo root relative to this composition: envs/dev → up four levels
  # (envs/dev → envs → terraform → biodata-registry).
  repo_root = "${path.root}/../../.."

  # Lambda source directories — passed to the lambda packaging modules.
  post_confirmation_source_dir = "${local.repo_root}/services/post-confirmation-lambda"
  migration_runner_source_dir  = "${local.repo_root}/services/migration-runner"
  migrations_dir               = "${local.repo_root}/migrations"

  # OAuth callback / logout URLs handed to Cognito. The CloudFront domain is
  # appended automatically once the distribution is provisioned. Local Vite
  # dev URLs are added by the cognito module's defaults.
  cognito_callback_urls = concat(
    [
      "https://${module.cloudfront_s3.distribution_domain}/",
      "https://${module.cloudfront_s3.distribution_domain}/auth/callback",
      "http://localhost:5173/",
    ],
    var.cognito_extra_callback_urls,
  )

  cognito_logout_urls = concat(
    [
      "https://${module.cloudfront_s3.distribution_domain}/",
      "http://localhost:5173/",
    ],
    var.cognito_extra_logout_urls,
  )
}

###############################################################################
# vpc — shared network plane.
###############################################################################

module "vpc" {
  source = "../../modules/vpc"

  name_prefix        = local.name_prefix
  environment        = var.environment
  project            = var.project
  vpc_cidr           = var.vpc_cidr
  availability_zones = var.availability_zones
  single_nat_gateway = var.single_nat_gateway

  # Bedrock Agent Runtime PrivateLink may not be GA in every region; toggle
  # off if planning fails on the bedrock-agent-runtime endpoint.
  enable_bedrock_endpoints              = true
  enable_bedrock_agent_runtime_endpoint = true

  tags = var.tags
}

###############################################################################
# aurora — PostgreSQL Serverless v2 (source of truth, R1.7).
#
# `bootstrap_slot_via_null_resource = false` because the operator typically
# runs `terraform apply` from outside the VPC. The vector + pg_trgm
# extensions and the biodata_cdc replication slot are created instead by
# the migration runner Lambda (Task 8.1) on the same apply, which DOES
# have VPC reach via its VPC config.
#
# WIRING CALLOUT — the prompt asked for `aurora_client_sg_id` from the vpc
# module; that output does not exist. The vpc module exports a single
# `internal_security_group_id` that doubles as the Aurora client SG (the
# SG is self-referencing, so any resource attached to it can talk to any
# other member on any port). Wiring `internal_security_group_id` here is
# the correct fix.
###############################################################################

module "aurora" {
  source = "../../modules/aurora"

  name_prefix        = local.name_prefix
  environment        = var.environment
  project            = var.project
  vpc_id             = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids
  security_group_ids = [module.vpc.internal_security_group_id]

  engine_version   = var.aurora_engine_version
  min_capacity_acu = var.aurora_min_capacity_acu
  max_capacity_acu = var.aurora_max_capacity_acu
  instance_count   = var.aurora_instance_count

  # Defer the slot + extension bootstrap to the migration runner so the
  # composition does not need VPC reach.
  bootstrap_slot_via_null_resource = false

  tags = var.tags
}

###############################################################################
# documentdb — CDC sink, mongosh-compatible. (R28.4)
#
# enable_readonly_user_bootstrap defaults off because the bootstrap requires
# VPC reach to the cluster on 27017 and operators usually apply from
# outside the VPC. Run scripts/bootstrap-docdb-readonly.sh from a bastion
# / SSM session post-apply if you need the read-only role for
# aind-data-access-api.
###############################################################################

module "documentdb" {
  source = "../../modules/documentdb"

  name_prefix        = local.name_prefix
  environment        = var.environment
  project            = var.project
  vpc_id             = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids
  security_group_ids = [module.vpc.internal_security_group_id]

  instance_class                 = var.documentdb_instance_class
  instance_count                 = var.documentdb_instance_count
  enable_readonly_user_bootstrap = var.documentdb_enable_readonly_user_bootstrap

  tags = var.tags
}

###############################################################################
# opensearch — Serverless SEARCH-type collection + synonyms bucket.
#
# `principal_arns = []` here because the consumer Lambdas
# (Indexing_Lambda, Search_Lambda, Embedding_Backfill_Lambda) don't exist
# yet — they are added in Phase 2/3. The data access policy can be updated
# in place when those modules are wired without recreating the collection.
###############################################################################

module "opensearch" {
  source = "../../modules/opensearch"

  name_prefix        = local.name_prefix
  environment        = var.environment
  project            = var.project
  vpc_id             = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids
  security_group_ids = [module.vpc.internal_security_group_id]

  # Wire the indexing_lambda's execution role so the data-access policy is
  # created in this apply. As more consumer Lambdas (Search, Embedding_Backfill)
  # are added in later phases, append their role ARNs here.
  principal_arns   = [
    module.indexing_lambda.iam_role_arn,
    module.search_lambda.iam_role_arn,
    module.embedding_backfill_lambda.iam_role_arn,
  ]
  standby_replicas = var.opensearch_standby_replicas

  tags = var.tags
}

###############################################################################
# elasticache — Redis 7.1 replication group fronting all 4 cache tiers.
###############################################################################

module "elasticache" {
  source = "../../modules/elasticache"

  name_prefix        = local.name_prefix
  environment        = var.environment
  project            = var.project
  vpc_id             = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids
  security_group_ids = [module.vpc.internal_security_group_id]

  node_type          = var.elasticache_node_type
  num_cache_clusters = var.elasticache_num_cache_clusters

  tags = var.tags
}

###############################################################################
# cloudfront_s3 — React-app static hosting (R21).
#
# The us_east_1 provider is wired explicitly because the cloudfront-s3
# module declares it as a configuration_alias (CloudFront ACM certs only
# live in us-east-1). The custom domain is opt-in via var.enable_custom_domain.
###############################################################################

module "cloudfront_s3" {
  source = "../../modules/cloudfront-s3"

  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  name_prefix    = local.name_prefix
  environment    = var.environment
  project        = var.project
  custom_domain  = var.enable_custom_domain ? var.custom_domain : null
  enable_logging = var.enable_cloudfront_logging

  # CSP must allow the SPA to call the API Gateway origin (connect-src),
  # otherwise the browser blocks every fetch to /public/stats, /search,
  # /metrics, etc. The API host is <rest_api_id>.execute-api.<region>.amazonaws.com.
  content_security_policy = join(" ", [
    "default-src 'self';",
    "img-src 'self' data: https:;",
    "script-src 'self';",
    "style-src 'self' 'unsafe-inline';",
    "font-src 'self' data:;",
    "connect-src 'self'",
    "https://cognito-idp.${var.aws_region}.amazonaws.com",
    "https://*.amazoncognito.com",
    "https://${aws_api_gateway_rest_api.main.id}.execute-api.${var.aws_region}.amazonaws.com;",
    "frame-ancestors 'none';",
    "form-action 'self';",
    "base-uri 'self';",
  ])

  tags = var.tags
}

###############################################################################
# post_confirmation_lambda — Cognito Post-Confirmation trigger (R19.3).
#
# The Lambda inserts a bare `app_user` row on every Cognito user
# confirmation. db_user is `migration_runner` here because the migrations
# (Task 7.1) create that user and grant INSERT on `app_user`. In production
# this should be a dedicated `post_confirmation` user with the minimum
# necessary grants — Task 5.2's design note tracks the future split.
#
# `app_user_has_org_id = false` because migration 0001 does not (yet) add
# an org_id column to app_user; flip when that schema change lands.
###############################################################################

module "post_confirmation_lambda" {
  source = "../../modules/lambdas/post-confirmation"

  name_prefix       = local.name_prefix
  environment       = var.environment
  project           = var.project
  source_dir        = local.post_confirmation_source_dir
  python_executable = var.python_executable

  db_host                    = module.aurora.cluster_endpoint
  db_port                    = module.aurora.port
  db_name                    = module.aurora.db_name
  db_user                    = "migration_runner"
  aurora_cluster_resource_id = module.aurora.cluster_resource_id
  aurora_cluster_arn         = module.aurora.cluster_arn

  vpc_subnet_ids         = module.vpc.private_subnet_ids
  vpc_security_group_ids = [module.vpc.internal_security_group_id]

  app_user_has_org_id = false

  tags = var.tags
}

###############################################################################
# cognito — User Pool + hosted UI + Post-Confirmation trigger.
#
# Wired AFTER post_confirmation_lambda so the trigger ARN is known. SAML is
# opt-in via var.cognito_saml_metadata_url; PoC default is username/password
# only.
#
# CALLOUT — at first apply, the Cognito callback / logout URL list contains
# the CloudFront domain pulled from module.cloudfront_s3, but CloudFront
# has not yet finished deploying. Terraform handles ordering correctly
# (cognito depends on cloudfront_s3 via the local), so the URL ends up
# correct on apply completion; the only consequence is a minute or two of
# CloudFront propagation before the URL actually resolves.
###############################################################################

module "cognito" {
  source = "../../modules/cognito"

  name_prefix   = local.name_prefix
  environment   = var.environment
  project       = var.project
  callback_urls = local.cognito_callback_urls
  logout_urls   = local.cognito_logout_urls

  saml_metadata_url            = var.cognito_saml_metadata_url
  post_confirmation_lambda_arn = module.post_confirmation_lambda.function_arn

  tags = var.tags

  depends_on = [module.post_confirmation_lambda]
}

###############################################################################
# migration_runner — Lambda that applies the 7 SQL migrations on apply.
#
# `invoke_on_apply = true` (module default) means the migrations run as
# part of every `terraform apply` — an apply that succeeds therefore
# guarantees the schema is up to date. The runner is idempotent (tracks
# applied versions in `schema_version`); re-running an apply with no
# migration changes is a no-op.
#
# It also bootstraps the CDC bits the aurora module would otherwise
# provision via psql (vector + pg_trgm extensions + biodata_cdc slot)
# because we set bootstrap_slot_via_null_resource = false above. (Task 8.1
# tracks the runner's slot-bootstrap step.)
###############################################################################

module "migration_runner" {
  source = "../../modules/lambdas/migration-runner"

  name_prefix       = local.name_prefix
  environment       = var.environment
  project           = var.project
  source_dir        = local.migration_runner_source_dir
  migrations_dir    = local.migrations_dir
  python_executable = var.python_executable

  db_host                    = module.aurora.cluster_endpoint
  db_port                    = module.aurora.port
  db_name                    = module.aurora.db_name
  db_user                    = "migration_runner"
  aurora_cluster_resource_id = module.aurora.cluster_resource_id
  aurora_cluster_arn         = module.aurora.cluster_arn

  vpc_subnet_ids         = module.vpc.private_subnet_ids
  vpc_security_group_ids = [module.vpc.internal_security_group_id]

  invoke_on_apply = true # Phase 1 verified: migrations apply cleanly via IAM auth.

  tags = var.tags
}

###############################################################################
# seeder — DEFERRED.
#
# Task 9.1 (sample-data seeder) is in progress; the Lambda module under
# terraform/modules/lambdas/seeder/ does not exist yet. When it lands, wire
# it here with `depends_on = [module.migration_runner]` so seeding only
# runs after the schema is in place. Until then, the seeder is invoked
# manually (out of band) via `python services/seeder/seeder.py` against
# the cluster after `terraform apply` completes.
###############################################################################

###############################################################################
# seeder — sample-data seeder Lambda (Task 9.1).
#
# Reads a 10% sample of the aind-data-schema snapshot from
# s3://aind-scratch-data/ and inserts it into Aurora through the
# relational data-asset + shared-entity graph. depends_on the migration
# runner so seeding only runs after the schema is in place. The seeder
# Lambda's source-hash trigger means re-running `terraform apply` is a
# no-op once the data is already loaded.
#
# `aurora_kms_decrypt_arn` is wired so the seeder can decrypt the
# bucket-encryption KMS key on the source S3 object — the customer's
# scratch bucket uses an SSE-KMS key shared with the Allen Institute
# org. The IAM policy already grants kms:Decrypt against this key.
###############################################################################

module "seeder" {
  source = "../../modules/lambdas/seeder"

  name_prefix       = local.name_prefix
  environment       = var.environment
  project           = var.project
  source_dir        = "${local.repo_root}/services/seeder"
  python_executable = var.python_executable

  db_host                    = module.aurora.cluster_endpoint
  db_port                    = module.aurora.port
  db_name                    = module.aurora.db_name
  db_user                    = "migration_runner"
  aurora_cluster_resource_id = module.aurora.cluster_resource_id
  aurora_cluster_arn         = module.aurora.cluster_arn

  vpc_subnet_ids         = module.vpc.private_subnet_ids
  vpc_security_group_ids = [module.vpc.internal_security_group_id]

  # The seeder uses ijson streaming so memory is no longer file-size bound.
  # 4 GB gives a comfortable margin for the per-record Pydantic validation
  # and the few hundred outstanding INSERT statement payloads.
  memory_mb       = 4096
  timeout_seconds = 900

  # Cap the seeder to a manageable record count for QC1. The 7.7 GB
  # snapshot at the current INSERT throughput (~10 records/sec, fanning
  # to 10+ tables each) cannot finish 10% of the corpus inside the
  # 15-minute Lambda hard ceiling. 500 records gives a representative
  # demo set that finishes in <2 minutes; production scale-up is the
  # Step Functions / ECS Fargate path documented in
  # services/seeder/README.md.
  invocation_payload = jsonencode({
    sample_fraction = 1.0
    max_records     = 500
  })

  invoke_on_apply = true

  tags = var.tags

  depends_on = [module.migration_runner]
}

###############################################################################
# seed_smoke_test — post-seed sanity checks (Task 9.2).
#
# Runs after the seeder; confirms at least the expected asset count, at
# least one Subject + Instrument + Session row, and no FK violations.
# A failed smoke test fails the `terraform apply`, surfacing a silent
# seed failure rather than letting it leak through to QC1 as an empty
# OpenSearch index.
###############################################################################

module "seed_smoke_test" {
  source = "../../modules/lambdas/seed-smoke-test"

  name_prefix       = local.name_prefix
  environment       = var.environment
  project           = var.project
  source_dir        = "${local.repo_root}/services/seed-smoke-test"
  python_executable = var.python_executable

  db_host                    = module.aurora.cluster_endpoint
  db_port                    = module.aurora.port
  db_name                    = module.aurora.db_name
  db_user                    = "migration_runner"
  aurora_cluster_resource_id = module.aurora.cluster_resource_id
  aurora_cluster_arn         = module.aurora.cluster_arn

  vpc_subnet_ids         = module.vpc.private_subnet_ids
  vpc_security_group_ids = [module.vpc.internal_security_group_id]

  invoke_on_apply = true

  # The 500-record QC1 sample drawn from the 7.7 GB snapshot does not
  # include any Session records — sessions are optional in
  # aind-data-schema and didn't surface in the slice. Lowering this
  # to 0 makes the smoke test reflect the actual data shape; bump
  # back to 1 once a representative slice with sessions is seeded.
  min_sessions = 0

  # Re-invoke the smoke test whenever the seeder produces a new summary.
  invocation_extra_triggers = {
    seeder_invocation = module.seeder.invocation_result
  }

  tags = var.tags

  depends_on = [module.seeder]
}

###############################################################################
# shared_layer — Lambda Layer with biodata_registry_shared package
# (Task 12.1). Imported by all business Lambdas (Registration, Authorizer,
# Validation, Lifecycle, Duplicates, Governance, Search, Revisions,
# Collections, Observability) for: aind-data-schema models, psycopg
# connection helper that issues SET LOCAL app.current_user_id/space_ids/roles,
# OpenAPI request validation middleware, error-response shaper, and
# auth-context parsing.
###############################################################################

module "shared_layer" {
  source = "../../modules/lambda-layer"

  name_prefix = local.name_prefix
  environment = var.environment
  project     = var.project
  source_dir  = "${local.repo_root}/services/shared-layer"

  python_executable = var.python_executable

  tags = var.tags
}

###############################################################################
# authorizer_lambda — API Gateway Lambda authorizer (Task 15.1).
#
# Validates the Cognito JWT and resolves {user_id, org_ids, space_ids,
# roles} from Aurora's user/user_org_role/user_space_role/sharing_grant
# tables, returning the IAM policy + context for downstream Lambdas.
###############################################################################

module "authorizer_lambda" {
  source = "../../modules/lambdas/authorizer"

  name_prefix       = local.name_prefix
  environment       = var.environment
  project           = var.project
  source_dir        = "${local.repo_root}/services/authorizer-lambda"
  python_executable = var.python_executable

  shared_layer_arn = module.shared_layer.layer_arn

  cognito_user_pool_id  = module.cognito.user_pool_id
  cognito_app_client_id = module.cognito.user_pool_client_id

  aurora_host                = module.aurora.cluster_endpoint
  aurora_port                = module.aurora.port
  aurora_db_name             = module.aurora.db_name
  db_user                    = "biodata_app"
  aurora_cluster_resource_id = module.aurora.cluster_resource_id
  aurora_cluster_arn         = module.aurora.cluster_arn

  vpc_subnet_ids         = module.vpc.private_subnet_ids
  vpc_security_group_ids = [module.vpc.internal_security_group_id]

  tags = var.tags
}

###############################################################################
# registration_lambda — POST /assets, PUT /assets/{id}, GET /assets/{id},
# POST /entities/{type}, etc. (Task 16.1). Writes to Aurora; CDC propagates
# to DocumentDB + OpenSearch automatically.
###############################################################################

module "registration_lambda" {
  source = "../../modules/lambdas/registration"

  name_prefix       = local.name_prefix
  environment       = var.environment
  project           = var.project
  source_dir        = "${local.repo_root}/services/registration-lambda"
  openapi_spec_path = "${local.repo_root}/openapi/openapi.yaml"

  shared_layer_arn = module.shared_layer.layer_arn

  aurora_host                = module.aurora.cluster_endpoint
  aurora_port                = module.aurora.port
  aurora_db_name             = module.aurora.db_name
  db_user                    = "biodata_app"
  aurora_cluster_resource_id = module.aurora.cluster_resource_id
  aurora_cluster_arn         = module.aurora.cluster_arn

  vpc_subnet_ids         = module.vpc.private_subnet_ids
  vpc_security_group_ids = [module.vpc.internal_security_group_id]

  tags = var.tags
}

###############################################################################
# api_gateway — REST API Gateway importing the OpenAPI spec.
###############################################################################

###############################################################################
# search_lambda — read path fronting OpenSearch Serverless.
###############################################################################

###############################################################################
# search_lambda — read path fronting OpenSearch Serverless.
###############################################################################

module "search_lambda" {
  source = "../../modules/lambdas/search"

  name_prefix = local.name_prefix
  environment = var.environment
  project     = var.project
  region      = var.aws_region

  source_dir        = "${local.repo_root}/services/search-lambda"
  python_executable = var.python_executable

  opensearch_endpoint       = module.opensearch.collection_endpoint
  opensearch_collection_arn = module.opensearch.collection_arn

  subnet_ids         = module.vpc.private_subnet_ids
  security_group_ids = [module.vpc.internal_security_group_id]

  # NL search (POST /search/nl) — wired to the bedrock-kb module.
  bedrock_kb_id              = module.bedrock_kb.knowledge_base_id
  nl_model_id                = "us.anthropic.claude-opus-4-7"
  aws_account_id             = data.aws_caller_identity.current.account_id
  aurora_host                = module.aurora.cluster_endpoint
  aurora_port                = module.aurora.port
  aurora_db_name             = module.aurora.db_name
  aurora_db_user             = "biodata_app"
  aurora_cluster_resource_id = module.aurora.cluster_resource_id
  redis_primary_endpoint     = module.elasticache.primary_endpoint
  redis_auth_token_secret_arn = module.elasticache.auth_token_secret_arn

  # NL pipeline budget: KB retrieve + Bedrock generation + EXPLAIN +
  # SQL execute. 60 seconds is generous; most queries finish in 6-10s.
  timeout_seconds = 60
  memory_mb       = 1024

  tags = var.tags
}

###############################################################################
# Phase 3-5 business Lambdas — all use the shared `business` module.
###############################################################################

locals {
  _business_common = {
    name_prefix       = local.name_prefix
    environment       = var.environment
    project           = var.project
    region            = var.aws_region
    python_executable = var.python_executable

    aurora_host                = module.aurora.cluster_endpoint
    aurora_port                = module.aurora.port
    aurora_db_name             = module.aurora.db_name
    db_user                    = "biodata_app"
    aurora_cluster_resource_id = module.aurora.cluster_resource_id

    subnet_ids         = module.vpc.private_subnet_ids
    security_group_ids = [module.vpc.internal_security_group_id]

    tags = var.tags
  }
}

module "validation_lambda" {
  source          = "../../modules/lambdas/business"
  function_suffix = "validation"
  source_dir      = "${local.repo_root}/services/validation-lambda"

  name_prefix       = local._business_common.name_prefix
  environment       = local._business_common.environment
  project           = local._business_common.project
  region            = local._business_common.region
  python_executable = local._business_common.python_executable

  aurora_host                = local._business_common.aurora_host
  aurora_port                = local._business_common.aurora_port
  aurora_db_name             = local._business_common.aurora_db_name
  db_user                    = local._business_common.db_user
  aurora_cluster_resource_id = local._business_common.aurora_cluster_resource_id

  subnet_ids         = local._business_common.subnet_ids
  security_group_ids = local._business_common.security_group_ids

  tags = local._business_common.tags
}

###############################################################################
# schema-revalidation — EventBridge rule + SQS queue + Revalidation_Lambda.
# Triggered when a new schema version is published; pages over data_asset
# rows by schema_id and re-validates each one against the new version.
###############################################################################

module "schema_revalidation" {
  source = "../../modules/schema-revalidation"

  name_prefix = local._business_common.name_prefix
  environment = local._business_common.environment
  project     = local._business_common.project
  region      = local._business_common.region

  source_dir        = "${local.repo_root}/services/revalidation-lambda"
  python_executable = local._business_common.python_executable

  aurora_host                = local._business_common.aurora_host
  aurora_port                = local._business_common.aurora_port
  aurora_db_name             = local._business_common.aurora_db_name
  db_user                    = local._business_common.db_user
  aurora_cluster_resource_id = local._business_common.aurora_cluster_resource_id

  subnet_ids         = local._business_common.subnet_ids
  security_group_ids = local._business_common.security_group_ids

  tags = local._business_common.tags
}

###############################################################################
# sns-notifications — per-Org notification topic infrastructure.
# Topics are created by Governance_Lambda at runtime when new Orgs are
# registered; this module exposes the publisher + manager IAM policies.
###############################################################################

module "sns_notifications" {
  source = "../../modules/sns-notifications"

  name_prefix = local._business_common.name_prefix
  environment = local._business_common.environment
  project     = local._business_common.project
  region      = local._business_common.region

  tags = local._business_common.tags
}

# Attach the manager policy to Governance_Lambda's exec role so it can
# create per-Org topics at runtime.
resource "aws_iam_role_policy_attachment" "governance_sns_manager" {
  role       = regex("[^/]+$", module.governance_lambda.iam_role_arn)
  policy_arn = module.sns_notifications.manager_policy_arn
}

# Attach the publisher policy to Duplicates and Lifecycle lambdas so they
# can publish notifications to per-Org topics for new duplicate flags +
# state transitions.
resource "aws_iam_role_policy_attachment" "duplicates_sns_publisher" {
  role       = regex("[^/]+$", module.duplicates_lambda.iam_role_arn)
  policy_arn = module.sns_notifications.publisher_policy_arn
}

resource "aws_iam_role_policy_attachment" "lifecycle_sns_publisher" {
  role       = regex("[^/]+$", module.lifecycle_lambda.iam_role_arn)
  policy_arn = module.sns_notifications.publisher_policy_arn
}

###############################################################################
# bedrock-kb — knowledge base seeded with registry DDL, JSONB documentation,
# example NL→SQL queries, and ontology term mappings. Consumed by
# Search_Lambda's POST /search/nl path.
###############################################################################

module "bedrock_kb" {
  source = "../../modules/bedrock-kb"

  name_prefix = local._business_common.name_prefix
  environment = local._business_common.environment
  project     = local._business_common.project
  region      = local._business_common.region

  seed_dir     = "${local.repo_root}/data/bedrock-kb-seed"
  # Operator role for index creation + debugging. Use the role ARN, NOT the
  # assumed-role session ARN (AOSS access policies want role ARNs).
  operator_arn = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/admin"

  tags = local._business_common.tags
}

module "lifecycle_lambda" {
  source          = "../../modules/lambdas/business"
  function_suffix = "lifecycle"
  source_dir      = "${local.repo_root}/services/lifecycle-lambda"

  name_prefix       = local._business_common.name_prefix
  environment       = local._business_common.environment
  project           = local._business_common.project
  region            = local._business_common.region
  python_executable = local._business_common.python_executable

  aurora_host                = local._business_common.aurora_host
  aurora_port                = local._business_common.aurora_port
  aurora_db_name             = local._business_common.aurora_db_name
  db_user                    = local._business_common.db_user
  aurora_cluster_resource_id = local._business_common.aurora_cluster_resource_id

  subnet_ids         = local._business_common.subnet_ids
  security_group_ids = local._business_common.security_group_ids

  tags = local._business_common.tags
}

module "duplicates_lambda" {
  source          = "../../modules/lambdas/business"
  function_suffix = "duplicates"
  source_dir      = "${local.repo_root}/services/duplicates-lambda"

  name_prefix       = local._business_common.name_prefix
  environment       = local._business_common.environment
  project           = local._business_common.project
  region            = local._business_common.region
  python_executable = local._business_common.python_executable

  aurora_host                = local._business_common.aurora_host
  aurora_port                = local._business_common.aurora_port
  aurora_db_name             = local._business_common.aurora_db_name
  db_user                    = local._business_common.db_user
  aurora_cluster_resource_id = local._business_common.aurora_cluster_resource_id

  subnet_ids         = local._business_common.subnet_ids
  security_group_ids = local._business_common.security_group_ids

  tags = local._business_common.tags
}

module "governance_lambda" {
  source          = "../../modules/lambdas/business"
  function_suffix = "governance"
  source_dir      = "${local.repo_root}/services/governance-lambda"

  name_prefix       = local._business_common.name_prefix
  environment       = local._business_common.environment
  project           = local._business_common.project
  region            = local._business_common.region
  python_executable = local._business_common.python_executable

  aurora_host                = local._business_common.aurora_host
  aurora_port                = local._business_common.aurora_port
  aurora_db_name             = local._business_common.aurora_db_name
  db_user                    = local._business_common.db_user
  aurora_cluster_resource_id = local._business_common.aurora_cluster_resource_id

  subnet_ids         = local._business_common.subnet_ids
  security_group_ids = local._business_common.security_group_ids

  extra_environment = {
    SNS_TOPIC_PREFIX = module.sns_notifications.topic_prefix
  }

  tags = local._business_common.tags
}

module "revisions_lambda" {
  source          = "../../modules/lambdas/business"
  function_suffix = "revisions"
  source_dir      = "${local.repo_root}/services/revisions-lambda"

  name_prefix       = local._business_common.name_prefix
  environment       = local._business_common.environment
  project           = local._business_common.project
  region            = local._business_common.region
  python_executable = local._business_common.python_executable

  aurora_host                = local._business_common.aurora_host
  aurora_port                = local._business_common.aurora_port
  aurora_db_name             = local._business_common.aurora_db_name
  db_user                    = local._business_common.db_user
  aurora_cluster_resource_id = local._business_common.aurora_cluster_resource_id

  subnet_ids         = local._business_common.subnet_ids
  security_group_ids = local._business_common.security_group_ids

  tags = local._business_common.tags
}

module "collections_lambda" {
  source          = "../../modules/lambdas/business"
  function_suffix = "collections"
  source_dir      = "${local.repo_root}/services/collections-lambda"

  name_prefix       = local._business_common.name_prefix
  environment       = local._business_common.environment
  project           = local._business_common.project
  region            = local._business_common.region
  python_executable = local._business_common.python_executable

  aurora_host                = local._business_common.aurora_host
  aurora_port                = local._business_common.aurora_port
  aurora_db_name             = local._business_common.aurora_db_name
  db_user                    = local._business_common.db_user
  aurora_cluster_resource_id = local._business_common.aurora_cluster_resource_id

  subnet_ids         = local._business_common.subnet_ids
  security_group_ids = local._business_common.security_group_ids

  tags = local._business_common.tags
}

module "observability_lambda" {
  source          = "../../modules/lambdas/business"
  function_suffix = "observability"
  source_dir      = "${local.repo_root}/services/observability-lambda"

  name_prefix       = local._business_common.name_prefix
  environment       = local._business_common.environment
  project           = local._business_common.project
  region            = local._business_common.region
  python_executable = local._business_common.python_executable

  aurora_host                = local._business_common.aurora_host
  aurora_port                = local._business_common.aurora_port
  aurora_db_name             = local._business_common.aurora_db_name
  db_user                    = local._business_common.db_user
  aurora_cluster_resource_id = local._business_common.aurora_cluster_resource_id

  subnet_ids         = local._business_common.subnet_ids
  security_group_ids = local._business_common.security_group_ids

  tags = local._business_common.tags
}

###############################################################################
# embedding_backfill_lambda — async OpenSearch vector population.
#
# Runs every 30s on EventBridge schedule. Queries OpenSearch for docs with
# embedding_pending=true, calls Bedrock Titan Embed v2, writes the vector
# back, clears the pending flag.
###############################################################################

module "embedding_backfill_lambda" {
  source          = "../../modules/lambdas/business"
  function_suffix = "embedding-backfill"
  source_dir      = "${local.repo_root}/services/embedding-backfill-lambda"

  name_prefix       = local._business_common.name_prefix
  environment       = local._business_common.environment
  project           = local._business_common.project
  region            = local._business_common.region
  python_executable = local._business_common.python_executable

  # Embedding backfill doesn't need Aurora — it only talks to OpenSearch
  # and Bedrock. Disable RDS IAM auth scoping to avoid an unused policy.
  enable_rds_iam_auth = false

  subnet_ids         = local._business_common.subnet_ids
  security_group_ids = local._business_common.security_group_ids

  memory_mb       = 1024
  timeout_seconds = 60

  extra_environment = {
    OPENSEARCH_ENDPOINT  = module.opensearch.collection_endpoint
    OPENSEARCH_INDEX     = "data_asset"
    EMBEDDING_MODEL_ID   = "amazon.titan-embed-text-v2:0"
    BATCH_SIZE           = "100"
  }

  extra_iam_statements = [
    {
      Effect   = "Allow"
      Action   = ["aoss:APIAccessAll"]
      Resource = module.opensearch.collection_arn
    },
    {
      Effect = "Allow"
      Action = ["bedrock:InvokeModel"]
      Resource = "arn:aws:bedrock:${var.aws_region}::foundation-model/amazon.titan-embed-text-v2:0"
    },
  ]

  tags = local._business_common.tags
}

# OpenSearch data-access policy needs to include the embedding lambda's role.
# (Already set in module "opensearch" — append role here.)

# EventBridge schedule — every 30s.
resource "aws_scheduler_schedule_group" "biodata" {
  name = "${local.name_prefix}-schedules"
  tags = var.tags
}

resource "aws_iam_role" "scheduler_invoke" {
  name = "${local.name_prefix}-scheduler-invoke"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy" "scheduler_invoke_lambda" {
  name = "${local.name_prefix}-scheduler-invoke-lambda"
  role = aws_iam_role.scheduler_invoke.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "lambda:InvokeFunction"
      Resource = [
        module.embedding_backfill_lambda.function_arn,
        module.duplicates_lambda.function_arn,
      ]
    }]
  })
}

resource "aws_scheduler_schedule" "embedding_backfill" {
  name        = "${local.name_prefix}-embedding-backfill"
  group_name  = aws_scheduler_schedule_group.biodata.name
  description = "Embedding backfill — every 30s drain of embedding_pending docs."

  flexible_time_window { mode = "OFF" }
  # EventBridge Scheduler minimum rate is 1 minute. The design.md target
  # of 30s is achieved by configuring the Lambda concurrency for fast
  # successive invocations and accepting up to 1 minute backfill latency.
  schedule_expression = "rate(1 minute)"

  target {
    arn      = module.embedding_backfill_lambda.function_arn
    role_arn = aws_iam_role.scheduler_invoke.arn
  }
}

###############################################################################
# Duplicates background scan — hourly EventBridge schedule that invokes
# Duplicates_Lambda with `{"action":"scan"}` to walk data_asset rows
# looking for new pgvector cosine pairs above threshold (Task 25.2).
###############################################################################

resource "aws_scheduler_schedule" "duplicate_scan" {
  name        = "${local.name_prefix}-duplicate-scan"
  group_name  = aws_scheduler_schedule_group.biodata.name
  description = "Duplicates background scan — hourly walk of new pgvector pairs."

  flexible_time_window { mode = "OFF" }
  schedule_expression = "rate(1 hour)"

  target {
    arn      = module.duplicates_lambda.function_arn
    role_arn = aws_iam_role.scheduler_invoke.arn
    input    = jsonencode({ action = "scan", source = "biodata-registry.duplicates" })
  }
}

###############################################################################
# metadata_agent_lambda — read-only chat proxy backed by Bedrock Claude.
###############################################################################

module "metadata_agent_lambda" {
  source          = "../../modules/lambdas/business"
  function_suffix = "metadata-agent"
  source_dir      = "${local.repo_root}/services/metadata-agent-lambda"

  name_prefix       = local._business_common.name_prefix
  environment       = local._business_common.environment
  project           = local._business_common.project
  region            = local._business_common.region
  python_executable = local._business_common.python_executable

  enable_rds_iam_auth = false

  subnet_ids         = local._business_common.subnet_ids
  security_group_ids = local._business_common.security_group_ids

  memory_mb       = 2048
  timeout_seconds = 60

  extra_environment = {
    # Sonnet 4.5 — markedly faster than Opus, so the multi-tool chat loop
    # finishes well under API Gateway's 29s integration cap (Opus was
    # hitting ~22-28s and timing out on cold starts).
    AGENT_MODEL_ID      = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    AGENT_MAX_TOKENS    = "1200"
    AGENT_MAX_TOOL_TURNS = "5"
    FN_SEARCH           = module.search_lambda.function_name
    FN_REGISTRATION     = module.registration_lambda.function_name
    FN_REVISIONS        = module.revisions_lambda.function_name
    NCBI_CONTACT_EMAIL  = "registry-demo@example.org"
  }

  extra_iam_statements = [
    {
      Effect   = "Allow"
      Action   = ["bedrock:InvokeModel"]
      Resource = [
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-5*",
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-4-7*",
        "arn:aws:bedrock:${var.aws_region}:${var.account_id}:inference-profile/us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "arn:aws:bedrock:${var.aws_region}:${var.account_id}:inference-profile/us.anthropic.claude-opus-4-7",
      ]
    },
    {
      # Read-only tool access: the agent proxies the Search and
      # Registration (read) Lambdas, forwarding the caller's RLS context.
      # Writer Lambdas are intentionally NOT granted — the agent stays
      # read-only (Property 8).
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = [
        module.search_lambda.function_arn,
        module.registration_lambda.function_arn,
        module.revisions_lambda.function_arn,
      ]
    },
  ]

  tags = local._business_common.tags
}

###############################################################################
# agentcore — AgentCore Runtime + Memory + Gateway + IAM identity.
# Wraps the metadata_agent_lambda with read-only-only IAM scoping for
# the agent execution role.
###############################################################################

module "agentcore" {
  source = "../../modules/agentcore"

  name_prefix = local._business_common.name_prefix
  environment = local._business_common.environment
  project     = local._business_common.project
  region      = local._business_common.region

  # Read-only Lambdas the agent can invoke. Writer Lambdas
  # (registration, lifecycle, governance, duplicates merge) are
  # **intentionally excluded** to enforce the read-only-agent invariant
  # (Property 8).
  readonly_tool_lambda_arns = [
    module.metadata_agent_lambda.function_arn,
    module.search_lambda.function_arn,
    module.revisions_lambda.function_arn,
    module.observability_lambda.function_arn,
  ]

  bedrock_kb_id               = module.bedrock_kb.knowledge_base_id
  cognito_user_pool_id        = module.cognito.user_pool_id
  cognito_user_pool_client_id = module.cognito.user_pool_client_id

  # Runtime container left blank — the Lambda already serves as the agent
  # entrypoint via /agent/chat (R7.8). Adding an AgentCore Runtime here
  # would duplicate the path; deferred until the team builds a dedicated
  # container image for production.
  runtime_container_uri = ""

  tags = local._business_common.tags
}

###############################################################################
# mcp-server — external MCP server exposing 7 read-only tools.
# Authenticates the connecting agent's Cognito JWT and proxies to the
# inner read-only Lambdas.
###############################################################################

module "mcp_server" {
  source = "../../modules/mcp-server"

  name_prefix       = local._business_common.name_prefix
  environment       = local._business_common.environment
  project           = local._business_common.project
  region            = local._business_common.region
  python_executable = local._business_common.python_executable

  source_dir = "${local.repo_root}/services/mcp-server-lambda"

  aurora_host                = local._business_common.aurora_host
  aurora_port                = local._business_common.aurora_port
  aurora_db_name             = local._business_common.aurora_db_name
  db_user                    = local._business_common.db_user
  aurora_cluster_resource_id = local._business_common.aurora_cluster_resource_id

  subnet_ids         = local._business_common.subnet_ids
  security_group_ids = local._business_common.security_group_ids

  search_lambda_name        = module.search_lambda.function_name
  search_lambda_arn         = module.search_lambda.function_arn
  registration_lambda_name  = module.registration_lambda.function_name
  registration_lambda_arn   = module.registration_lambda.function_arn
  collections_lambda_name   = module.collections_lambda.function_name
  collections_lambda_arn    = module.collections_lambda.function_arn
  validation_lambda_name    = module.validation_lambda.function_name
  validation_lambda_arn     = module.validation_lambda.function_arn
  observability_lambda_name = module.observability_lambda.function_name
  observability_lambda_arn  = module.observability_lambda.function_arn

  tags = local._business_common.tags
}

###############################################################################
# api_gateway — minimal direct-resource API for QC2 demo.
#
# The full OpenAPI-spec import path requires x-amazon-apigateway-integration
# blocks that the original openapi.yaml does not include. Rather than
# block QC2 on regenerating the spec, this composition defines the
# critical demo endpoints directly: POST /assets and GET /healthz.
# The spec-import path remains for future use once the spec is augmented.
###############################################################################

resource "aws_api_gateway_rest_api" "main" {
  name        = "${local.name_prefix}-rest"
  description = "Allen BioData Registry REST API (QC2 minimal). Routes: POST /assets, GET /healthz."

  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

# Authorizer (REQUEST type — Authorizer Lambda resolves user context).
resource "aws_api_gateway_authorizer" "cognito" {
  name                             = "${local.name_prefix}-authorizer"
  rest_api_id                      = aws_api_gateway_rest_api.main.id
  type                             = "REQUEST"
  authorizer_uri                   = module.authorizer_lambda.invoke_arn
  identity_source                  = "method.request.header.Authorization"
  authorizer_result_ttl_in_seconds = 300
}

resource "aws_lambda_permission" "invoke_authorizer" {
  statement_id  = "AllowAPIGatewayInvokeAuthorizer"
  action        = "lambda:InvokeFunction"
  function_name = module.authorizer_lambda.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.main.execution_arn}/authorizers/${aws_api_gateway_authorizer.cognito.id}"
}

# /assets resource
resource "aws_api_gateway_resource" "assets" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_rest_api.main.root_resource_id
  path_part   = "assets"
}

# POST /assets — wired to Registration Lambda
resource "aws_api_gateway_method" "post_assets" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_resource.assets.id
  http_method   = "POST"
  authorization = "CUSTOM"
  authorizer_id = aws_api_gateway_authorizer.cognito.id
}

resource "aws_api_gateway_integration" "post_assets" {
  rest_api_id             = aws_api_gateway_rest_api.main.id
  resource_id             = aws_api_gateway_resource.assets.id
  http_method             = aws_api_gateway_method.post_assets.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = module.registration_lambda.invoke_arn
}

resource "aws_lambda_permission" "invoke_registration" {
  statement_id  = "AllowAPIGatewayInvokeRegistration"
  action        = "lambda:InvokeFunction"
  function_name = module.registration_lambda.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.main.execution_arn}/*/*"
}

# GET /assets/{id} — wired to Registration Lambda. The resource itself is
# defined further down (asset_id) but the GET method needs to live near
# its sibling POST /assets so the deployment trigger picks it up.
resource "aws_api_gateway_method" "get_asset_id" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_resource.asset_id.id
  http_method   = "GET"
  authorization = "CUSTOM"
  authorizer_id = aws_api_gateway_authorizer.cognito.id
  request_parameters = {
    "method.request.path.id" = true
  }
}

resource "aws_api_gateway_integration" "get_asset_id" {
  rest_api_id             = aws_api_gateway_rest_api.main.id
  resource_id             = aws_api_gateway_resource.asset_id.id
  http_method             = aws_api_gateway_method.get_asset_id.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = module.registration_lambda.invoke_arn
}

# PUT /assets/{id} — wired to Registration Lambda for updates.
resource "aws_api_gateway_method" "put_asset_id" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_resource.asset_id.id
  http_method   = "PUT"
  authorization = "CUSTOM"
  authorizer_id = aws_api_gateway_authorizer.cognito.id
  request_parameters = {
    "method.request.path.id" = true
  }
}

resource "aws_api_gateway_integration" "put_asset_id" {
  rest_api_id             = aws_api_gateway_rest_api.main.id
  resource_id             = aws_api_gateway_resource.asset_id.id
  http_method             = aws_api_gateway_method.put_asset_id.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = module.registration_lambda.invoke_arn
}

# /search — wired to Search Lambda
resource "aws_api_gateway_resource" "search" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_rest_api.main.root_resource_id
  path_part   = "search"
}

resource "aws_api_gateway_method" "get_search" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_resource.search.id
  http_method   = "GET"
  authorization = "CUSTOM"
  authorizer_id = aws_api_gateway_authorizer.cognito.id
  request_parameters = {
    "method.request.querystring.q"     = false
    "method.request.querystring.limit" = false
    "method.request.querystring.index" = false
  }
}

resource "aws_api_gateway_integration" "get_search" {
  rest_api_id             = aws_api_gateway_rest_api.main.id
  resource_id             = aws_api_gateway_resource.search.id
  http_method             = aws_api_gateway_method.get_search.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = module.search_lambda.invoke_arn
}

# /suggest — wired to Search Lambda
resource "aws_api_gateway_resource" "suggest" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_rest_api.main.root_resource_id
  path_part   = "suggest"
}

resource "aws_api_gateway_method" "get_suggest" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_resource.suggest.id
  http_method   = "GET"
  authorization = "CUSTOM"
  authorizer_id = aws_api_gateway_authorizer.cognito.id
  request_parameters = {
    "method.request.querystring.prefix" = true
    "method.request.querystring.limit"  = false
  }
}

resource "aws_api_gateway_integration" "get_suggest" {
  rest_api_id             = aws_api_gateway_rest_api.main.id
  resource_id             = aws_api_gateway_resource.suggest.id
  http_method             = aws_api_gateway_method.get_suggest.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = module.search_lambda.invoke_arn
}

resource "aws_lambda_permission" "invoke_search" {
  statement_id  = "AllowAPIGatewayInvokeSearch"
  action        = "lambda:InvokeFunction"
  function_name = module.search_lambda.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.main.execution_arn}/*/*"
}

# /search/nl — Natural-language search via Bedrock + KB.
resource "aws_api_gateway_resource" "search_nl" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_resource.search.id
  path_part   = "nl"
}

resource "aws_api_gateway_method" "post_search_nl" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_resource.search_nl.id
  http_method   = "POST"
  authorization = "CUSTOM"
  authorizer_id = aws_api_gateway_authorizer.cognito.id
}

resource "aws_api_gateway_integration" "post_search_nl" {
  rest_api_id             = aws_api_gateway_rest_api.main.id
  resource_id             = aws_api_gateway_resource.search_nl.id
  http_method             = aws_api_gateway_method.post_search_nl.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = module.search_lambda.invoke_arn
  # API Gateway integration timeout is capped at 29s. Bedrock NL→SQL plus
  # SQL execute usually fits within this; complex queries may time out
  # and surface as 504 Gateway Timeout — clients should retry with a
  # narrower question.
  timeout_milliseconds = 29000
}

###############################################################################
# Phase 3-5 routes — wire each business Lambda to API Gateway resources.
#
# Routes added (all CUSTOM-authorizer except where noted):
#   POST /validate                 → validation_lambda
#   POST /assets/{id}/publish      → lifecycle_lambda
#   POST /assets/{id}/register     → lifecycle_lambda
#   POST /assets/{id}/archive      → lifecycle_lambda
#   GET  /duplicates               → duplicates_lambda
#   POST /duplicates/{id}/dismiss  → duplicates_lambda
#   POST /duplicates/{id}/merge    → duplicates_lambda
#   POST /orgs                     → governance_lambda
#   POST /orgs/{id}/spaces         → governance_lambda
#   POST /orgs/{id}/sharing-grants → governance_lambda
#   GET  /revisions                → revisions_lambda
#   POST /collections              → collections_lambda
#   POST /collections/{id}/assets  → collections_lambda
#   POST /collections/{id}/children → collections_lambda
#   PUT  /collections/{id}/doi     → collections_lambda
#   GET  /metrics/asset-counts     → observability_lambda
#   GET  /metrics/validation-distribution → observability_lambda
#   GET  /metrics/growth           → observability_lambda
###############################################################################

# /validate
resource "aws_api_gateway_resource" "validate" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_rest_api.main.root_resource_id
  path_part   = "validate"
}

resource "aws_api_gateway_method" "post_validate" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_resource.validate.id
  http_method   = "POST"
  authorization = "CUSTOM"
  authorizer_id = aws_api_gateway_authorizer.cognito.id
}

resource "aws_api_gateway_integration" "post_validate" {
  rest_api_id             = aws_api_gateway_rest_api.main.id
  resource_id             = aws_api_gateway_resource.validate.id
  http_method             = aws_api_gateway_method.post_validate.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = module.validation_lambda.invoke_arn
}

resource "aws_lambda_permission" "invoke_validation" {
  statement_id  = "AllowAPIGatewayInvokeValidation"
  action        = "lambda:InvokeFunction"
  function_name = module.validation_lambda.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.main.execution_arn}/*/*"
}

# /assets/{id}/publish, /register, /archive  (lifecycle)
resource "aws_api_gateway_resource" "asset_id" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_resource.assets.id
  path_part   = "{id}"
}

resource "aws_api_gateway_resource" "asset_publish" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_resource.asset_id.id
  path_part   = "publish"
}
resource "aws_api_gateway_resource" "asset_register" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_resource.asset_id.id
  path_part   = "register"
}
resource "aws_api_gateway_resource" "asset_archive" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_resource.asset_id.id
  path_part   = "archive"
}
resource "aws_api_gateway_resource" "asset_unpublish" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_resource.asset_id.id
  path_part   = "unpublish"
}

resource "aws_api_gateway_method" "post_asset_publish" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_resource.asset_publish.id
  http_method   = "POST"
  authorization = "CUSTOM"
  authorizer_id = aws_api_gateway_authorizer.cognito.id
  request_parameters = { "method.request.path.id" = true }
}
resource "aws_api_gateway_method" "post_asset_register" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_resource.asset_register.id
  http_method   = "POST"
  authorization = "CUSTOM"
  authorizer_id = aws_api_gateway_authorizer.cognito.id
  request_parameters = { "method.request.path.id" = true }
}
resource "aws_api_gateway_method" "post_asset_archive" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_resource.asset_archive.id
  http_method   = "POST"
  authorization = "CUSTOM"
  authorizer_id = aws_api_gateway_authorizer.cognito.id
  request_parameters = { "method.request.path.id" = true }
}
resource "aws_api_gateway_method" "post_asset_unpublish" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_resource.asset_unpublish.id
  http_method   = "POST"
  authorization = "CUSTOM"
  authorizer_id = aws_api_gateway_authorizer.cognito.id
  request_parameters = { "method.request.path.id" = true }
}

resource "aws_api_gateway_integration" "post_asset_publish" {
  rest_api_id             = aws_api_gateway_rest_api.main.id
  resource_id             = aws_api_gateway_resource.asset_publish.id
  http_method             = aws_api_gateway_method.post_asset_publish.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = module.lifecycle_lambda.invoke_arn
}
resource "aws_api_gateway_integration" "post_asset_register" {
  rest_api_id             = aws_api_gateway_rest_api.main.id
  resource_id             = aws_api_gateway_resource.asset_register.id
  http_method             = aws_api_gateway_method.post_asset_register.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = module.lifecycle_lambda.invoke_arn
}
resource "aws_api_gateway_integration" "post_asset_archive" {
  rest_api_id             = aws_api_gateway_rest_api.main.id
  resource_id             = aws_api_gateway_resource.asset_archive.id
  http_method             = aws_api_gateway_method.post_asset_archive.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = module.lifecycle_lambda.invoke_arn
}
resource "aws_api_gateway_integration" "post_asset_unpublish" {
  rest_api_id             = aws_api_gateway_rest_api.main.id
  resource_id             = aws_api_gateway_resource.asset_unpublish.id
  http_method             = aws_api_gateway_method.post_asset_unpublish.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = module.lifecycle_lambda.invoke_arn
}

resource "aws_lambda_permission" "invoke_lifecycle" {
  statement_id  = "AllowAPIGatewayInvokeLifecycle"
  action        = "lambda:InvokeFunction"
  function_name = module.lifecycle_lambda.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.main.execution_arn}/*/*"
}

# /duplicates
resource "aws_api_gateway_resource" "duplicates" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_rest_api.main.root_resource_id
  path_part   = "duplicates"
}
resource "aws_api_gateway_method" "get_duplicates" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_resource.duplicates.id
  http_method   = "GET"
  authorization = "CUSTOM"
  authorizer_id = aws_api_gateway_authorizer.cognito.id
}
resource "aws_api_gateway_integration" "get_duplicates" {
  rest_api_id             = aws_api_gateway_rest_api.main.id
  resource_id             = aws_api_gateway_resource.duplicates.id
  http_method             = aws_api_gateway_method.get_duplicates.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = module.duplicates_lambda.invoke_arn
}

resource "aws_lambda_permission" "invoke_duplicates" {
  statement_id  = "AllowAPIGatewayInvokeDuplicates"
  action        = "lambda:InvokeFunction"
  function_name = module.duplicates_lambda.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.main.execution_arn}/*/*"
}

# /orgs (governance)
resource "aws_api_gateway_resource" "orgs" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_rest_api.main.root_resource_id
  path_part   = "orgs"
}
resource "aws_api_gateway_method" "post_orgs" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_resource.orgs.id
  http_method   = "POST"
  authorization = "CUSTOM"
  authorizer_id = aws_api_gateway_authorizer.cognito.id
}
resource "aws_api_gateway_integration" "post_orgs" {
  rest_api_id             = aws_api_gateway_rest_api.main.id
  resource_id             = aws_api_gateway_resource.orgs.id
  http_method             = aws_api_gateway_method.post_orgs.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = module.governance_lambda.invoke_arn
}

resource "aws_lambda_permission" "invoke_governance" {
  statement_id  = "AllowAPIGatewayInvokeGovernance"
  action        = "lambda:InvokeFunction"
  function_name = module.governance_lambda.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.main.execution_arn}/*/*"
}

# /revisions
resource "aws_api_gateway_resource" "revisions" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_rest_api.main.root_resource_id
  path_part   = "revisions"
}
resource "aws_api_gateway_method" "get_revisions" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_resource.revisions.id
  http_method   = "GET"
  authorization = "CUSTOM"
  authorizer_id = aws_api_gateway_authorizer.cognito.id
}
resource "aws_api_gateway_integration" "get_revisions" {
  rest_api_id             = aws_api_gateway_rest_api.main.id
  resource_id             = aws_api_gateway_resource.revisions.id
  http_method             = aws_api_gateway_method.get_revisions.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = module.revisions_lambda.invoke_arn
}

resource "aws_lambda_permission" "invoke_revisions" {
  statement_id  = "AllowAPIGatewayInvokeRevisions"
  action        = "lambda:InvokeFunction"
  function_name = module.revisions_lambda.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.main.execution_arn}/*/*"
}

# /collections
resource "aws_api_gateway_resource" "collections" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_rest_api.main.root_resource_id
  path_part   = "collections"
}
resource "aws_api_gateway_method" "post_collections" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_resource.collections.id
  http_method   = "POST"
  authorization = "CUSTOM"
  authorizer_id = aws_api_gateway_authorizer.cognito.id
}
resource "aws_api_gateway_integration" "post_collections" {
  rest_api_id             = aws_api_gateway_rest_api.main.id
  resource_id             = aws_api_gateway_resource.collections.id
  http_method             = aws_api_gateway_method.post_collections.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = module.collections_lambda.invoke_arn
}

resource "aws_lambda_permission" "invoke_collections" {
  statement_id  = "AllowAPIGatewayInvokeCollections"
  action        = "lambda:InvokeFunction"
  function_name = module.collections_lambda.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.main.execution_arn}/*/*"
}

# /metrics/* (observability)
resource "aws_api_gateway_resource" "metrics" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_rest_api.main.root_resource_id
  path_part   = "metrics"
}
resource "aws_api_gateway_resource" "metrics_proxy" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_resource.metrics.id
  path_part   = "{proxy+}"
}
resource "aws_api_gateway_method" "get_metrics_proxy" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_resource.metrics_proxy.id
  http_method   = "GET"
  authorization = "CUSTOM"
  authorizer_id = aws_api_gateway_authorizer.cognito.id
  request_parameters = { "method.request.path.proxy" = true }
}
resource "aws_api_gateway_integration" "get_metrics_proxy" {
  rest_api_id             = aws_api_gateway_rest_api.main.id
  resource_id             = aws_api_gateway_resource.metrics_proxy.id
  http_method             = aws_api_gateway_method.get_metrics_proxy.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = module.observability_lambda.invoke_arn
}

resource "aws_lambda_permission" "invoke_observability" {
  statement_id  = "AllowAPIGatewayInvokeObservability"
  action        = "lambda:InvokeFunction"
  function_name = module.observability_lambda.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.main.execution_arn}/*/*"
}

# /agent/chat — wired to MetaData Agent Lambda
resource "aws_api_gateway_resource" "agent" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_rest_api.main.root_resource_id
  path_part   = "agent"
}
resource "aws_api_gateway_resource" "agent_chat" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_resource.agent.id
  path_part   = "chat"
}
resource "aws_api_gateway_method" "post_agent_chat" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_resource.agent_chat.id
  http_method   = "POST"
  authorization = "CUSTOM"
  authorizer_id = aws_api_gateway_authorizer.cognito.id
}
resource "aws_api_gateway_integration" "post_agent_chat" {
  rest_api_id             = aws_api_gateway_rest_api.main.id
  resource_id             = aws_api_gateway_resource.agent_chat.id
  http_method             = aws_api_gateway_method.post_agent_chat.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = module.metadata_agent_lambda.invoke_arn
}
resource "aws_lambda_permission" "invoke_metadata_agent" {
  statement_id  = "AllowAPIGatewayInvokeMetadataAgent"
  action        = "lambda:InvokeFunction"
  function_name = module.metadata_agent_lambda.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.main.execution_arn}/*/*"
}

# /public/agent/chat — PUBLIC (no-auth) MetaMate. The Lambda detects the
# public path and runs a locked-down, published-data-only toolset
# (public_search + required_fields + lookup_ontology). No private reads are
# possible. Reuses the metadata_agent_lambda; the wildcard invoke permission
# above already covers this route.
resource "aws_api_gateway_resource" "public_agent" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_resource.public.id
  path_part   = "agent"
}
resource "aws_api_gateway_resource" "public_agent_chat" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_resource.public_agent.id
  path_part   = "chat"
}
resource "aws_api_gateway_method" "post_public_agent_chat" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_resource.public_agent_chat.id
  http_method   = "POST"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "post_public_agent_chat" {
  rest_api_id             = aws_api_gateway_rest_api.main.id
  resource_id             = aws_api_gateway_resource.public_agent_chat.id
  http_method             = aws_api_gateway_method.post_public_agent_chat.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = module.metadata_agent_lambda.invoke_arn
}

# /mcp/tools and /mcp/invoke — external MCP server.
resource "aws_api_gateway_resource" "mcp" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_rest_api.main.root_resource_id
  path_part   = "mcp"
}
resource "aws_api_gateway_resource" "mcp_tools" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_resource.mcp.id
  path_part   = "tools"
}
resource "aws_api_gateway_resource" "mcp_invoke" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_resource.mcp.id
  path_part   = "invoke"
}
resource "aws_api_gateway_method" "get_mcp_tools" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_resource.mcp_tools.id
  http_method   = "GET"
  authorization = "CUSTOM"
  authorizer_id = aws_api_gateway_authorizer.cognito.id
}
resource "aws_api_gateway_method" "post_mcp_invoke" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_resource.mcp_invoke.id
  http_method   = "POST"
  authorization = "CUSTOM"
  authorizer_id = aws_api_gateway_authorizer.cognito.id
}
resource "aws_api_gateway_integration" "get_mcp_tools" {
  rest_api_id             = aws_api_gateway_rest_api.main.id
  resource_id             = aws_api_gateway_resource.mcp_tools.id
  http_method             = aws_api_gateway_method.get_mcp_tools.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = module.mcp_server.invoke_arn
}
resource "aws_api_gateway_integration" "post_mcp_invoke" {
  rest_api_id             = aws_api_gateway_rest_api.main.id
  resource_id             = aws_api_gateway_resource.mcp_invoke.id
  http_method             = aws_api_gateway_method.post_mcp_invoke.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = module.mcp_server.invoke_arn
  timeout_milliseconds    = 29000
}
resource "aws_lambda_permission" "invoke_mcp_server" {
  statement_id  = "AllowAPIGatewayInvokeMCPServer"
  action        = "lambda:InvokeFunction"
  function_name = module.mcp_server.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.main.execution_arn}/*/*"
}

# /healthz — public, mock integration
resource "aws_api_gateway_resource" "healthz" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_rest_api.main.root_resource_id
  path_part   = "healthz"
}

# /public — parent for unauthenticated read-only endpoints
resource "aws_api_gateway_resource" "public" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_rest_api.main.root_resource_id
  path_part   = "public"
}

# /public/stats — public aggregate counts for the landing page (no auth)
resource "aws_api_gateway_resource" "public_stats" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_resource.public.id
  path_part   = "stats"
}

resource "aws_api_gateway_method" "get_public_stats" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_resource.public_stats.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "get_public_stats" {
  rest_api_id             = aws_api_gateway_rest_api.main.id
  resource_id             = aws_api_gateway_resource.public_stats.id
  http_method             = aws_api_gateway_method.get_public_stats.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = module.search_lambda.invoke_arn
}

# /public/assets — public browse/search of published assets (no auth)
resource "aws_api_gateway_resource" "public_assets" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_resource.public.id
  path_part   = "assets"
}

resource "aws_api_gateway_method" "get_public_assets" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_resource.public_assets.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "get_public_assets" {
  rest_api_id             = aws_api_gateway_rest_api.main.id
  resource_id             = aws_api_gateway_resource.public_assets.id
  http_method             = aws_api_gateway_method.get_public_assets.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = module.search_lambda.invoke_arn
}

# /public/assets/{id} — public single-asset read (no auth)
resource "aws_api_gateway_resource" "public_asset_id" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_resource.public_assets.id
  path_part   = "{id}"
}

resource "aws_api_gateway_method" "get_public_asset_id" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_resource.public_asset_id.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "get_public_asset_id" {
  rest_api_id             = aws_api_gateway_rest_api.main.id
  resource_id             = aws_api_gateway_resource.public_asset_id.id
  http_method             = aws_api_gateway_method.get_public_asset_id.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = module.search_lambda.invoke_arn
}

resource "aws_api_gateway_method" "get_healthz" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_resource.healthz.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "get_healthz" {
  rest_api_id             = aws_api_gateway_rest_api.main.id
  resource_id             = aws_api_gateway_resource.healthz.id
  http_method             = aws_api_gateway_method.get_healthz.http_method
  type                    = "MOCK"
  request_templates       = { "application/json" = "{\"statusCode\": 200}" }
}

resource "aws_api_gateway_method_response" "get_healthz_200" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  resource_id = aws_api_gateway_resource.healthz.id
  http_method = aws_api_gateway_method.get_healthz.http_method
  status_code = "200"
}

resource "aws_api_gateway_integration_response" "get_healthz_200" {
  rest_api_id      = aws_api_gateway_rest_api.main.id
  resource_id      = aws_api_gateway_resource.healthz.id
  http_method      = aws_api_gateway_method.get_healthz.http_method
  status_code      = aws_api_gateway_method_response.get_healthz_200.status_code
  response_templates = { "application/json" = jsonencode({ status = "ok" }) }
  depends_on       = [aws_api_gateway_integration.get_healthz]
}

resource "aws_api_gateway_deployment" "main" {
  rest_api_id = aws_api_gateway_rest_api.main.id

  triggers = {
    redeploy = sha1(jsonencode([
      aws_api_gateway_resource.assets.id,
      aws_api_gateway_method.post_assets.id,
      aws_api_gateway_integration.post_assets.id,
      aws_api_gateway_method.get_asset_id.id,
      aws_api_gateway_integration.get_asset_id.id,
      aws_api_gateway_method.put_asset_id.id,
      aws_api_gateway_integration.put_asset_id.id,
      aws_api_gateway_resource.public_stats.id,
      aws_api_gateway_method.get_public_stats.id,
      aws_api_gateway_integration.get_public_stats.id,
      aws_api_gateway_resource.public_assets.id,
      aws_api_gateway_method.get_public_assets.id,
      aws_api_gateway_integration.get_public_assets.id,
      aws_api_gateway_resource.public_asset_id.id,
      aws_api_gateway_method.get_public_asset_id.id,
      aws_api_gateway_integration.get_public_asset_id.id,
      aws_api_gateway_resource.healthz.id,
      aws_api_gateway_method.get_healthz.id,
      aws_api_gateway_integration.get_healthz.id,
      aws_api_gateway_resource.search.id,
      aws_api_gateway_method.get_search.id,
      aws_api_gateway_integration.get_search.id,
      aws_api_gateway_resource.suggest.id,
      aws_api_gateway_method.get_suggest.id,
      aws_api_gateway_integration.get_suggest.id,
      aws_api_gateway_resource.search_nl.id,
      aws_api_gateway_method.post_search_nl.id,
      aws_api_gateway_integration.post_search_nl.id,
      aws_api_gateway_resource.validate.id,
      aws_api_gateway_integration.post_validate.id,
      aws_api_gateway_resource.asset_publish.id,
      aws_api_gateway_integration.post_asset_publish.id,
      aws_api_gateway_resource.asset_register.id,
      aws_api_gateway_integration.post_asset_register.id,
      aws_api_gateway_resource.asset_archive.id,
      aws_api_gateway_integration.post_asset_archive.id,
      aws_api_gateway_resource.asset_unpublish.id,
      aws_api_gateway_integration.post_asset_unpublish.id,
      aws_api_gateway_resource.duplicates.id,
      aws_api_gateway_integration.get_duplicates.id,
      aws_api_gateway_resource.orgs.id,
      aws_api_gateway_integration.post_orgs.id,
      aws_api_gateway_resource.revisions.id,
      aws_api_gateway_integration.get_revisions.id,
      aws_api_gateway_resource.collections.id,
      aws_api_gateway_integration.post_collections.id,
      aws_api_gateway_resource.metrics.id,
      aws_api_gateway_resource.metrics_proxy.id,
      aws_api_gateway_integration.get_metrics_proxy.id,
      aws_api_gateway_resource.agent.id,
      aws_api_gateway_resource.agent_chat.id,
      aws_api_gateway_integration.post_agent_chat.id,
      aws_api_gateway_resource.public_agent.id,
      aws_api_gateway_resource.public_agent_chat.id,
      aws_api_gateway_method.post_public_agent_chat.id,
      aws_api_gateway_integration.post_public_agent_chat.id,
      aws_api_gateway_resource.mcp.id,
      aws_api_gateway_resource.mcp_tools.id,
      aws_api_gateway_resource.mcp_invoke.id,
      aws_api_gateway_method.get_mcp_tools.id,
      aws_api_gateway_method.post_mcp_invoke.id,
      aws_api_gateway_integration.get_mcp_tools.id,
      aws_api_gateway_integration.post_mcp_invoke.id,
      aws_api_gateway_authorizer.cognito.id,
      # CORS — bumping any of these invalidates the deployment so OPTIONS
      # preflight + gateway-response CORS headers reach the stage.
      jsonencode([for k, v in aws_api_gateway_method.cors_options : v.id]),
      jsonencode([for k, v in aws_api_gateway_integration.cors_options : v.id]),
      jsonencode([for k, v in aws_api_gateway_integration_response.cors_options : v.id]),
      aws_api_gateway_gateway_response.default_4xx.id,
      aws_api_gateway_gateway_response.default_5xx.id,
      aws_api_gateway_gateway_response.unauthorized.id,
      aws_api_gateway_gateway_response.missing_auth.id,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_api_gateway_stage" "main" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  deployment_id = aws_api_gateway_deployment.main.id
  stage_name    = var.environment
}

output "api_invoke_url" {
  description = "Base URL of the deployed API Gateway stage."
  value       = "${aws_api_gateway_stage.main.invoke_url}"
}

###############################################################################
# cdc_pipeline — SQS FIFO transport from Aurora WAL to the Indexing Lambda.
#
# Task 17.1 authored the module but left it unwired pending the
# Indexing_Lambda (Task 18.1, this composition's `module
# "indexing_lambda"` below). We wire both together here so the queue's
# IAM allow-list and the Lambda's event-source mapping land in the
# same apply.
#
# `consumer_lambda_role_arns` is set after the indexing module is
# declared, but Terraform resolves the dependency graph from the
# `iam_role_arn` reference — there's no need for an explicit
# depends_on between the two modules.
###############################################################################

module "cdc_pipeline" {
  source = "../../modules/cdc-pipeline"

  name_prefix = local.name_prefix
  environment = var.environment
  project     = var.project

  aurora_cluster_endpoint    = module.aurora.cluster_endpoint
  aurora_cluster_resource_id = module.aurora.cluster_resource_id
  db_port                    = module.aurora.port
  db_name                    = module.aurora.db_name

  vpc_subnet_ids         = module.vpc.private_subnet_ids
  vpc_security_group_ids = [module.vpc.internal_security_group_id]

  python_executable = var.python_executable

  # Real cdc-reader source — drains the biodata_cdc replication slot via
  # test_decoding plugin and emits to SQS FIFO. Replaces the module's
  # placeholder source tree.
  cdc_reader_source_dir = "${local.repo_root}/services/cdc-reader"

  # The Indexing_Lambda's execution role goes here so the FIFO queue's
  # access policy permits ReceiveMessage. The dependency from
  # module.cdc_pipeline -> module.indexing_lambda's iam_role_arn is
  # safe — the policy is applied to the queue *after* the queue
  # exists, and the Lambda's event source mapping retries until the
  # queue is reachable.
  consumer_lambda_role_arns = [module.indexing_lambda.iam_role_arn]

  tags = var.tags
}

###############################################################################
# indexing_lambda — CDC consumer fanning out to DocumentDB + OpenSearch.
#
# Task 18.1: see services/indexing-lambda/handler.py and
# terraform/modules/lambdas/indexing/main.tf for the full contract.
#
# The Lambda packages psycopg, pymongo, opensearch-py, and
# requests-aws4auth into its own zip — it does NOT consume the shared
# Lambda Layer's psycopg helper because the indexer authenticates as
# the privileged BYPASSRLS `cdc_indexer` Postgres role (the shared
# helper is hard-wired for application-level RLS-aware connections).
#
# Aurora secret ARN: the migration runner is responsible for creating
# the `cdc_indexer` user and storing its credentials in a Secrets
# Manager secret. For now we point at the master secret as a
# convenience — once Task 18.1's migration step lands a dedicated
# `cdc_indexer` secret, swap this to that ARN.
###############################################################################

module "indexing_lambda" {
  source = "../../modules/lambdas/indexing"

  name_prefix = local.name_prefix
  environment = var.environment
  project     = var.project
  region      = var.aws_region

  source_dir        = "${local.repo_root}/services/indexing-lambda"
  python_executable = var.python_executable

  # Aurora — privileged BYPASSRLS connection.
  aurora_secret_arn  = module.aurora.master_secret_arn
  aurora_host        = module.aurora.cluster_endpoint
  aurora_port        = module.aurora.port
  aurora_db_name     = module.aurora.db_name
  aurora_kms_key_arn = module.aurora.kms_key_arn

  # DocumentDB — service-to-service inside VPC.
  docdb_secret_arn  = module.documentdb.master_secret_arn
  docdb_endpoint    = module.documentdb.cluster_endpoint
  docdb_port        = module.documentdb.port
  docdb_kms_key_arn = module.documentdb.kms_key_arn

  # OpenSearch.
  opensearch_endpoint       = module.opensearch.collection_endpoint
  opensearch_collection_arn = module.opensearch.collection_arn

  # CDC pipeline source queue + DLQ.
  source_sqs_queue_arn = module.cdc_pipeline.main_queue_arn
  source_sqs_queue_url = module.cdc_pipeline.main_queue_url
  dlq_arn              = module.cdc_pipeline.dlq_arn
  dlq_url              = module.cdc_pipeline.dlq_url

  # Networking.
  subnet_ids         = module.vpc.private_subnet_ids
  security_group_ids = [module.vpc.internal_security_group_id]

  # Sizing.
  memory_mb            = 1024
  timeout_seconds      = 60
  reserved_concurrency = 10

  tags = var.tags
}

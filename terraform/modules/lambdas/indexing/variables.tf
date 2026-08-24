###############################################################################
# Variables — lambdas/indexing module
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

variable "tags" {
  description = "Additional tags merged onto every resource."
  type        = map(string)
  default     = {}
}

variable "region" {
  description = "AWS region. Used for IAM resource ARN composition (rds-db:connect, OpenSearch SigV4) and for the Lambda's `OPENSEARCH_REGION` env var."
  type        = string
  default     = "us-west-2"
}

###############################################################################
# Source / packaging
###############################################################################

variable "source_dir" {
  description = "Absolute path to the Lambda source directory (containing handler.py + requirements.txt). The dev composition typically supplies '$${path.module}/../../../services/indexing-lambda'."
  type        = string
}

variable "build_dir" {
  description = "Absolute path to a working directory the module owns for staging the deployment package. Anything under this path may be deleted and recreated on every apply. Defaults to a per-module temp directory under the calling Terraform working directory."
  type        = string
  default     = null
}

variable "python_executable" {
  description = "Python executable used to install runtime dependencies into the staging directory. Should match the Lambda runtime (3.12). Defaults to `python3`. NOTE: psycopg-binary, pymongo, and friends ship Linux x86_64 wheels — building on macOS or Windows produces a zip that won't run on Lambda. The recommended pattern is to set this to `docker run public.ecr.aws/lambda/python:3.12 pip ...` in production CI."
  type        = string
  default     = "python3"
}

variable "shared_layer_arn" {
  description = "Optional ARN of the shared Lambda Layer (biodata_registry_shared, Task 12.1). The Indexing Lambda does NOT depend on the shared Layer for its core path — psycopg (with BYPASSRLS), pymongo, opensearch-py and requests-aws4auth ship in this Lambda's own zip. The Layer is attached only when set so error-shaping and structured-logging helpers are available; pass null to skip."
  type        = string
  default     = null
}

###############################################################################
# Aurora connection
###############################################################################

variable "aurora_secret_arn" {
  description = "Secrets Manager ARN holding the `cdc_indexer` Aurora user credentials (JSON: username, password, engine, host, port, dbname). The handler reads SecretId on cold start and caches the result. The IAM execution role is granted `secretsmanager:GetSecretValue` on this ARN."
  type        = string
}

variable "aurora_host" {
  description = "Aurora writer endpoint hostname. From `module.aurora.cluster_endpoint`. Passed via env var so the Lambda can connect without resolving the secret first."
  type        = string
}

variable "aurora_port" {
  description = "Aurora TCP port (5432 for PostgreSQL)."
  type        = number
  default     = 5432
}

variable "aurora_db_name" {
  description = "Aurora database name. Defaults to whatever the Aurora secret declares; pass an explicit override here if the cluster hosts more than one logical database."
  type        = string
  default     = null
}

variable "aurora_kms_key_arn" {
  description = "ARN of the KMS CMK encrypting the Aurora secret + cluster. Used to add `kms:Decrypt` to the Lambda's execution role. Pass null when Aurora uses AWS-managed keys."
  type        = string
  default     = null
}

###############################################################################
# DocumentDB connection
###############################################################################

variable "docdb_secret_arn" {
  description = "Secrets Manager ARN holding the DocumentDB cluster master credentials. The Indexing Lambda authenticates as the master user (a service-to-service trust path inside the VPC); IAM auth is reserved for external aind-data-access-api consumers. The IAM role is granted `secretsmanager:GetSecretValue` on this ARN."
  type        = string
}

variable "docdb_endpoint" {
  description = "DocumentDB cluster endpoint hostname. From `module.documentdb.cluster_endpoint`."
  type        = string
}

variable "docdb_port" {
  description = "DocumentDB port. Defaults to 27017 (MongoDB compatibility default)."
  type        = number
  default     = 27017
}

variable "docdb_kms_key_arn" {
  description = "ARN of the KMS CMK encrypting the DocumentDB secret + cluster. Used to add `kms:Decrypt` to the Lambda's execution role. Pass null when DocumentDB uses AWS-managed keys."
  type        = string
  default     = null
}

###############################################################################
# OpenSearch
###############################################################################

variable "opensearch_endpoint" {
  description = "OpenSearch Serverless collection endpoint URL (https://...). The Lambda strips the protocol before passing to opensearch-py. From `module.opensearch.collection_endpoint`."
  type        = string
}

variable "opensearch_collection_arn" {
  description = "ARN of the OpenSearch Serverless collection. Used to scope the IAM `aoss:APIAccessAll` policy. From `module.opensearch.collection_arn`."
  type        = string
}

###############################################################################
# CDC pipeline — source SQS queue + DLQ
###############################################################################

variable "source_sqs_queue_arn" {
  description = "ARN of the SQS FIFO queue to consume from (the main queue from the cdc-pipeline module). Used as the event source mapping target and to scope the Lambda's `sqs:ReceiveMessage`/`DeleteMessage`/`GetQueueAttributes` policy."
  type        = string
}

variable "source_sqs_queue_url" {
  description = "URL of the SQS FIFO queue. Documented for symmetry with source_sqs_queue_arn; the Lambda itself uses the ARN for receive but the URL is useful for diagnostics."
  type        = string
  default     = null
}

variable "dlq_url" {
  description = "URL of the DLQ where failed events are enqueued (tagged by target). The handler reads this from the `DLQ_URL` env var on every failed write."
  type        = string
}

variable "dlq_arn" {
  description = "ARN of the DLQ. Used to scope the Lambda's `sqs:SendMessage` policy. Without this, a misconfigured Lambda would silently fail to enqueue DLQ events."
  type        = string
}

###############################################################################
# Networking
###############################################################################

variable "vpc_id" {
  description = "VPC ID the Lambda runs in. Documented for completeness; the Lambda itself only needs subnet_ids and security_group_ids."
  type        = string
  default     = null
}

variable "subnet_ids" {
  description = "Private subnet IDs the Lambda runs in. Must include subnets that route to Aurora, DocumentDB, and the OpenSearch Serverless VPC endpoint. From `module.vpc.private_subnet_ids`."
  type        = list(string)

  validation {
    condition     = length(var.subnet_ids) > 0
    error_message = "At least one private subnet ID is required."
  }
}

variable "security_group_ids" {
  description = "Security group IDs attached to the Lambda's ENIs. The SGs must permit egress to Aurora (5432), DocumentDB (27017), and OpenSearch (443). Reuses `module.vpc.internal_security_group_id` in the dev composition."
  type        = list(string)

  validation {
    condition     = length(var.security_group_ids) > 0
    error_message = "At least one security group ID is required."
  }
}

###############################################################################
# Runtime / sizing
###############################################################################

variable "memory_mb" {
  description = "Lambda memory size in MB. 1024 MB is the design.md default for the indexer — gives ~2 vCPU equivalents which speeds up the JOIN-heavy hydration path. Scale up if the per-batch latency budget is tight."
  type        = number
  default     = 1024

  validation {
    condition     = var.memory_mb >= 128 && var.memory_mb <= 10240
    error_message = "memory_mb must be between 128 and 10240."
  }
}

variable "timeout_seconds" {
  description = "Lambda timeout. 60 seconds matches the design's `main_visibility_timeout_seconds=300` headroom — a single batch must complete well before SQS redelivers. Bump if batch sizes grow."
  type        = number
  default     = 60

  validation {
    condition     = var.timeout_seconds >= 10 && var.timeout_seconds <= 900
    error_message = "timeout_seconds must be between 10 and 900."
  }
}

variable "reserved_concurrency" {
  description = "Reserved concurrency cap for the Indexing Lambda. PoC default is 10; this is enough headroom for the SQS FIFO throughput limit (300 msg/sec per group) without overwhelming Aurora's connection budget. Production should profile and adjust."
  type        = number
  default     = 10

  validation {
    condition     = var.reserved_concurrency == -1 || (var.reserved_concurrency >= 0 && var.reserved_concurrency <= 1000)
    error_message = "reserved_concurrency must be -1 (unreserved) or between 0 and 1000."
  }
}

variable "batch_size" {
  description = "SQS event source mapping batch size. 10 messages per invocation balances per-batch fixed cost (cold start, JOIN reuse) against per-message latency. The CDC pipeline produces small JSON payloads so 10 is well below the 256 KB total batch ceiling."
  type        = number
  default     = 10

  validation {
    condition     = var.batch_size >= 1 && var.batch_size <= 10
    error_message = "batch_size for FIFO SQS event sources must be between 1 and 10."
  }
}

variable "batch_window_seconds" {
  description = "SQS event source mapping batch window — how long Lambda waits to fill a batch before invoking. 5 seconds matches the design's CDC end-to-end latency budget (5s p99 for DocumentDB + OpenSearch lexical visibility). Tighten to 0 for the lowest possible latency at the cost of more invocations."
  type        = number
  default     = 5

  validation {
    condition     = var.batch_window_seconds >= 0 && var.batch_window_seconds <= 300
    error_message = "batch_window_seconds must be between 0 and 300."
  }
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the Lambda's log group. 14 days is the operator-friendly PoC default — long enough to investigate a CDC issue from the previous sprint, short enough to avoid runaway log spend."
  type        = number
  default     = 14
}

variable "log_level" {
  description = "Python logging level inside the Lambda."
  type        = string
  default     = "INFO"

  validation {
    condition     = contains(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], var.log_level)
    error_message = "log_level must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL."
  }
}

variable "lambda_kms_key_arn" {
  description = "Optional CMK ARN used to encrypt the Indexing Lambda's environment variables. When null, AWS-owned keys are used."
  type        = string
  default     = null
}

###############################################################################
# Alarms
###############################################################################

variable "error_alarm_threshold" {
  description = "Threshold for the Lambda invocation-error alarm. Default 5 errors over 5 minutes — picks up real failures without paging on transient ENI / cold-start hiccups. Tighten in production."
  type        = number
  default     = 5
}

variable "error_alarm_evaluation_periods" {
  description = "Evaluation periods for the invocation-error alarm. 1 period = 5-minute aggregation."
  type        = number
  default     = 1
}

variable "alarm_actions" {
  description = "List of SNS topic ARNs (or other CloudWatch alarm action ARNs) notified when an alarm fires. Empty list (PoC default) means alarms only show in the AWS console; production should pass an on-call SNS topic."
  type        = list(string)
  default     = []
}

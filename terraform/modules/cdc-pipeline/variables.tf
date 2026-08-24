###############################################################################
# Variables — cdc-pipeline module
#
# Inputs follow the conventions used by the aurora and lambdas/* modules.
# Defaults assume the Allen Institute dev environment (us-west-2).
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

###############################################################################
# Aurora source connection
#
# The CDC Reader Lambda connects to the writer endpoint via IAM database
# authentication (no password in env vars) and reads from the logical
# replication slot using the pgoutput plugin. Aurora's parameter group
# must already have rds.logical_replication = 1 (handled by the aurora
# module, Task 3.1) and the slot itself must already exist (handled by
# the migration runner, Task 8.1).
###############################################################################

variable "aurora_cluster_endpoint" {
  description = "Aurora writer endpoint hostname. Sourced from `module.aurora.cluster_endpoint`."
  type        = string

  validation {
    condition     = length(var.aurora_cluster_endpoint) > 0
    error_message = "aurora_cluster_endpoint is required."
  }
}

variable "aurora_cluster_resource_id" {
  description = "Immutable Aurora cluster resource id (`cluster-xxx`). Used to scope the IAM policy granting `rds-db:connect` to *this* cluster + DB user, not all clusters in the account. From `module.aurora.cluster_resource_id`."
  type        = string

  validation {
    condition     = length(var.aurora_cluster_resource_id) > 0
    error_message = "aurora_cluster_resource_id is required."
  }
}

variable "aurora_db_user_for_cdc" {
  description = "DB user the CDC Reader Lambda authenticates as via Aurora IAM database authentication. The user must (a) exist, (b) have membership in the `rds_iam` Aurora role, and (c) hold the REPLICATION attribute (`ALTER ROLE <user> WITH REPLICATION`). The migration runner is responsible for creating this user; this module only consumes it."
  type        = string
  default     = "cdc_reader"

  validation {
    condition     = can(regex("^[A-Za-z][A-Za-z0-9_]{0,62}$", var.aurora_db_user_for_cdc))
    error_message = "aurora_db_user_for_cdc must start with a letter and contain only letters, digits, and underscores."
  }
}

variable "db_port" {
  description = "Aurora TCP port (5432 for PostgreSQL)."
  type        = number
  default     = 5432
}

variable "db_name" {
  description = "Aurora database name. From `module.aurora.db_name`."
  type        = string
  default     = "biodata_registry"
}

variable "cdc_replication_slot_name" {
  description = "Name of the Aurora logical replication slot the CDC Reader Lambda consumes. The slot is created out-of-band by the migration runner / aurora bootstrap; this module only reads from it. Default `biodata_cdc` matches the aurora module's default."
  type        = string
  default     = "biodata_cdc"
}

variable "cdc_publication_name" {
  description = "Name of the PostgreSQL publication the pgoutput plugin filters by. Created by the migration runner alongside the replication slot. Default `biodata_cdc_pub` matches the migration corpus."
  type        = string
  default     = "biodata_cdc_pub"
}

###############################################################################
# Networking — for the CDC Reader Lambda
###############################################################################

variable "vpc_subnet_ids" {
  description = "Private subnet IDs the CDC Reader Lambda runs in. Must include subnets that route to Aurora — the Lambda needs network reach to the writer endpoint over port 5432. From `module.vpc.private_subnet_ids`."
  type        = list(string)

  validation {
    condition     = length(var.vpc_subnet_ids) > 0
    error_message = "At least one private subnet ID is required."
  }
}

variable "vpc_security_group_ids" {
  description = "Security group IDs attached to the CDC Reader Lambda's ENIs. The SGs must permit egress to Aurora's security group on port 5432. The dev composition typically reuses the internal SG from `module.vpc`."
  type        = list(string)

  validation {
    condition     = length(var.vpc_security_group_ids) > 0
    error_message = "At least one security group ID is required."
  }
}

###############################################################################
# SQS encryption + redrive
###############################################################################

variable "sqs_kms_key_arn" {
  description = "Optional ARN of a customer-managed KMS CMK used to encrypt both the main FIFO queue and the DLQ. When null (default), the SQS-managed `alias/aws/sqs` key is used. Production should pass the same CMK as the rest of the stack to keep key lifecycle centralized."
  type        = string
  default     = null
}

variable "max_receive_count" {
  description = "Number of failed receives by the Indexing_Lambda before SQS moves a message to the DLQ. 3 is the design.md default — gives enough headroom for transient DocumentDB / OpenSearch hiccups without burying truly broken events deep in the main queue. (R28.6.)"
  type        = number
  default     = 3

  validation {
    condition     = var.max_receive_count >= 1 && var.max_receive_count <= 1000
    error_message = "max_receive_count must be between 1 and 1000."
  }
}

variable "main_visibility_timeout_seconds" {
  description = "Visibility timeout on the main FIFO queue. 300s (5 min) matches the Indexing_Lambda's max timeout — the consumer Lambda's visibility window must be ≥ its own timeout to avoid duplicate deliveries on long invocations."
  type        = number
  default     = 300

  validation {
    condition     = var.main_visibility_timeout_seconds >= 0 && var.main_visibility_timeout_seconds <= 43200
    error_message = "main_visibility_timeout_seconds must be between 0 and 43200 (12h, SQS hard limit)."
  }
}

variable "main_message_retention_seconds" {
  description = "Retention on the main FIFO queue. 4 days (345600s) is the design default — enough headroom for a long weekend's outage of the Indexing_Lambda before messages start aging out. SQS hard ceiling is 14 days."
  type        = number
  default     = 345600

  validation {
    condition     = var.main_message_retention_seconds >= 60 && var.main_message_retention_seconds <= 1209600
    error_message = "main_message_retention_seconds must be between 60 (1 min) and 1209600 (14 days)."
  }
}

variable "dlq_message_retention_seconds" {
  description = "Retention on the DLQ. 14 days (1209600s, SQS max) so operators have a full sprint to investigate failures before messages are permanently lost. (R28.6.)"
  type        = number
  default     = 1209600

  validation {
    condition     = var.dlq_message_retention_seconds >= 60 && var.dlq_message_retention_seconds <= 1209600
    error_message = "dlq_message_retention_seconds must be between 60 (1 min) and 1209600 (14 days)."
  }
}

###############################################################################
# CDC Reader Lambda — runtime sizing
###############################################################################

variable "cdc_reader_source_dir" {
  description = "Optional absolute path to the CDC Reader Lambda source directory (containing handler.py, reader.py, requirements.txt). When null (PoC default), the module ships a tiny placeholder source tree it builds itself — enough to make `terraform validate` and `terraform plan` succeed without depending on services/cdc-reader/ existing yet. The placeholder Lambda logs a not-implemented message and exits cleanly; it does NOT actually drain the slot. Replace with a real source dir when services/cdc-reader/ lands as part of Task 18.x."
  type        = string
  default     = null
}

variable "build_dir" {
  description = "Absolute path to a working directory the module owns for staging the deployment package. Anything under this path may be deleted and recreated on every apply. Defaults to a per-module temp directory under the calling Terraform working directory."
  type        = string
  default     = null
}

variable "python_executable" {
  description = "Python executable used to install runtime dependencies into the staging directory. Should match the Lambda runtime (3.12). Defaults to `python3`."
  type        = string
  default     = "python3"
}

variable "cdc_reader_memory_mb" {
  description = "Memory for the CDC Reader Lambda. 512 MB is plenty for a slot-draining loop; the bottleneck is network I/O to Aurora, not CPU. Bump to 1024 MB+ if profiling shows the pgoutput parser benefiting from extra allocated CPU."
  type        = number
  default     = 512

  validation {
    condition     = var.cdc_reader_memory_mb >= 128 && var.cdc_reader_memory_mb <= 10240
    error_message = "cdc_reader_memory_mb must be between 128 and 10240."
  }
}

variable "cdc_reader_timeout_seconds" {
  description = "Timeout for the CDC Reader Lambda. The schedule fires every 60s by default; a 50s timeout leaves ~10s of headroom and ensures the next invocation does not collide with a hung previous run. Bump if the slot has a large backlog and a single drain takes longer."
  type        = number
  default     = 50

  validation {
    condition     = var.cdc_reader_timeout_seconds >= 30 && var.cdc_reader_timeout_seconds <= 900
    error_message = "cdc_reader_timeout_seconds must be between 30 and 900."
  }
}

variable "cdc_reader_schedule_expression" {
  description = "EventBridge Scheduler expression that fires the CDC Reader Lambda. PoC default `rate(1 minute)` matches the design's CDC latency budget (≤5s end-to-end is the *target* under the chosen MSK upgrade path; the Lambda-on-schedule fallback gives 1-min visibility, which is acceptable for the QC2 demo at PoC volume). Use `rate(30 seconds)` for a tighter visibility budget at the cost of more invocations (each costs a few ms of warm-runtime, immaterial)."
  type        = string
  default     = "rate(1 minute)"
}

variable "cdc_reader_log_retention_days" {
  description = "CloudWatch Logs retention for the CDC Reader Lambda's log group. 90 days is reasonable for the PoC."
  type        = number
  default     = 90
}

variable "cdc_reader_log_level" {
  description = "Python logging level inside the CDC Reader Lambda."
  type        = string
  default     = "INFO"

  validation {
    condition     = contains(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], var.cdc_reader_log_level)
    error_message = "cdc_reader_log_level must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL."
  }
}

variable "lambda_kms_key_arn" {
  description = "Optional CMK ARN used to encrypt the CDC Reader Lambda's environment variables. When null, AWS-owned keys are used."
  type        = string
  default     = null
}

###############################################################################
# Indexing_Lambda consumer — IAM allow-list
#
# The Indexing_Lambda (Task 18) is provisioned in a separate module. We
# accept its execution-role ARNs here and write them into the SQS queue
# policy so the consumer can `ReceiveMessage` / `DeleteMessage` /
# `GetQueueAttributes`. Empty list is valid: a queue with no consumer
# attached is a no-op, useful while the cdc-pipeline module is wired in
# before the Indexing_Lambda module exists.
###############################################################################

variable "consumer_lambda_role_arns" {
  description = "List of IAM role ARNs (typically the Indexing_Lambda's execution role) allowed to consume from the main FIFO queue and the DLQ. Empty list is supported — the queues are still provisioned but no consumer is wired in yet. Wire this list once the Indexing_Lambda module (Task 18.1) exists."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for arn in var.consumer_lambda_role_arns :
      can(regex("^arn:aws[a-zA-Z-]*:iam::[0-9]{12}:role/", arn))
    ])
    error_message = "Every consumer_lambda_role_arns entry must be an IAM role ARN."
  }
}

###############################################################################
# CloudWatch alarms
###############################################################################

variable "dlq_alarm_actions" {
  description = "List of SNS topic ARNs (or other CloudWatch alarm action ARNs) notified when the DLQ alarm fires. Empty list (PoC default) means the alarm goes to ALARM state visibly in the AWS console but does not page anyone — appropriate for the demo. Production must pass a real on-call SNS topic."
  type        = list(string)
  default     = []
}

variable "dlq_alarm_evaluation_periods" {
  description = "Number of consecutive 60-second periods the DLQ depth must exceed the threshold before the alarm fires. 1 = alarm on the first non-empty period, which is the design.md default for CDC failures (any DLQ drop is interesting)."
  type        = number
  default     = 1

  validation {
    condition     = var.dlq_alarm_evaluation_periods >= 1 && var.dlq_alarm_evaluation_periods <= 24
    error_message = "dlq_alarm_evaluation_periods must be between 1 and 24."
  }
}

variable "dlq_alarm_threshold" {
  description = "Threshold for `ApproximateNumberOfMessagesVisible` on the DLQ. The alarm fires when the metric is `> threshold`. Default 0 means any DLQ message triggers the alarm — appropriate for a CDC pipeline where a single failure indicates a bug or outage."
  type        = number
  default     = 0
}

###############################################################################
# Forward-compatibility: MSK upgrade path
#
# When CDC volume outgrows what an SQS-buffered Lambda can chew through,
# the design.md upgrade path is to swap to MSK Serverless with the
# Indexing_Lambda's interface unchanged at the module boundary. This
# variable is the explicit scaffolding for that swap — when set to true
# in a future PR, the module would (NOT in this Task 17.1) provision MSK
# Serverless instead of SQS. Today the variable is *exposed but unused*
# so consuming compositions can wire it through without churn later.
###############################################################################

variable "enable_msk_upgrade_path" {
  description = "Forward-compatibility flag. When true, future module versions WILL provision MSK Serverless (with the same Indexing_Lambda interface) instead of the SQS FIFO buffer wired today. NOT IMPLEMENTED in this task — setting true currently raises a validation error so callers get a clear signal. The variable is exposed now so dev / staging compositions can wire it through and flip the switch later without a module-API break."
  type        = bool
  default     = false

  validation {
    condition     = var.enable_msk_upgrade_path == false
    error_message = "enable_msk_upgrade_path = true is reserved for a future task. Today's module only implements the SQS FIFO buffer path. Leave this at the default `false`."
  }
}

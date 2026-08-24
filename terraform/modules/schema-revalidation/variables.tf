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

variable "source_dir" {
  description = "Path to services/revalidation-lambda (the handler.py + requirements.txt)."
  type        = string
}

variable "python_executable" {
  type    = string
  default = "python3"
}

variable "shared_layer_arn" {
  type    = string
  default = null
}

variable "event_bus_name" {
  description = "EventBridge bus to attach the schema.version.published rule to. Defaults to the account default bus."
  type        = string
  default     = "default"
}

# Aurora connection
variable "aurora_host" {
  type = string
}

variable "aurora_port" {
  type    = number
  default = 5432
}

variable "aurora_db_name" {
  type = string
}

variable "db_user" {
  type    = string
  default = "biodata_app"
}

variable "aurora_cluster_resource_id" {
  type = string
}

# VPC
variable "subnet_ids" {
  type = list(string)
}

variable "security_group_ids" {
  type = list(string)
}

# Sizing
variable "memory_mb" {
  type    = number
  default = 1024
}

variable "timeout_seconds" {
  description = "Lambda execution timeout. The visibility timeout on the SQS source queue is set to 6x this value."
  type        = number
  default     = 60
}

variable "log_level" {
  type    = string
  default = "INFO"
}

# SQS sizing
variable "batch_size" {
  description = "SQS event source mapping batch size. Standard queue, so up to 10 messages per invocation."
  type        = number
  default     = 10
  validation {
    condition     = var.batch_size >= 1 && var.batch_size <= 10
    error_message = "batch_size must be between 1 and 10 for non-FIFO SQS event sources."
  }
}

variable "max_receive_count" {
  description = "Number of times a message is delivered before moving to the DLQ."
  type        = number
  default     = 5
}

variable "per_asset_batch_size" {
  description = "Number of data_asset rows the Lambda processes per invocation when consuming a schema_published task. Tunes memory and execution time."
  type        = number
  default     = 50
}

variable "queue_depth_alarm_threshold" {
  description = "Visible-message threshold above which the queue depth alarm fires."
  type        = number
  default     = 1000
}

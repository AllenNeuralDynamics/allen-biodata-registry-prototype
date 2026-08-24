###############################################################################
# Variables — lambda-layer module
###############################################################################

variable "name_prefix" {
  description = "Prefix applied to the layer name and to every resource Name tag. Typically '<project>-<environment>', e.g. 'biodata-registry-dev'."
  type        = string
  default     = "biodata-registry-dev"

  validation {
    condition     = length(var.name_prefix) > 0 && length(var.name_prefix) <= 40
    error_message = "name_prefix must be 1–40 characters."
  }
}

variable "layer_name" {
  description = "Layer name suffix appended to name_prefix. The full layer name is '<name_prefix>-<layer_name>'. Default 'shared' produces 'biodata-registry-dev-shared'."
  type        = string
  default     = "shared"
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
  description = "Additional tags merged onto every resource. The aws_lambda_layer_version resource itself does not support tags (AWS API limitation), so these are applied only to ancillary resources (e.g. the SSM parameter that exposes the layer ARN)."
  type        = map(string)
  default     = {}
}

###############################################################################
# Source / packaging
###############################################################################

variable "source_dir" {
  description = "Absolute path to the shared-layer source directory (the directory containing the biodata_registry_shared/ Python package and requirements.txt). Typically '$${path.module}/../../../services/shared-layer' from the dev composition."
  type        = string
}

variable "package_subdir" {
  description = "Name of the in-source Python package directory copied into the layer. Default 'biodata_registry_shared' matches the package's __init__.py location. Override only if the package is renamed."
  type        = string
  default     = "biodata_registry_shared"
}

variable "build_dir" {
  description = "Absolute path to a working directory the module owns for staging the layer package. Anything under this path may be deleted and recreated on every apply. Defaults to a per-module temp directory under the calling Terraform working directory."
  type        = string
  default     = null
}

variable "python_executable" {
  description = "Python executable used to install runtime dependencies into the staging directory. Defaults to 'python3'. The interpreter version should match the Lambda runtime (3.12). psycopg[binary] ships precompiled manylinux2014 wheels that work on the Lambda Python 3.12 runtime (Amazon Linux 2023)."
  type        = string
  default     = "python3"
}

variable "compatible_runtimes" {
  description = "Lambda runtimes the layer is compatible with. python3.12 is the only runtime targeted by the registry's Lambdas; multi-runtime support is left for production hardening."
  type        = list(string)
  default     = ["python3.12"]

  validation {
    condition     = length(var.compatible_runtimes) > 0
    error_message = "compatible_runtimes must list at least one runtime."
  }
}

variable "compatible_architectures" {
  description = "Lambda CPU architectures the layer is compatible with. The default ['x86_64'] keeps the fastest path through psycopg's manylinux2014 wheels. Add 'arm64' once the layer is rebuilt on Graviton — psycopg's binary wheels also exist for aarch64 but the build pipeline must compile under ARM to pick them up."
  type        = list(string)
  default     = ["x86_64"]

  validation {
    condition = length(var.compatible_architectures) > 0 && alltrue([
      for arch in var.compatible_architectures : contains(["x86_64", "arm64"], arch)
    ])
    error_message = "compatible_architectures must contain only 'x86_64' or 'arm64'."
  }
}

variable "description" {
  description = "Human-readable description of the layer, displayed in the AWS Lambda console. Surfaced in audit trails when consumers query which layer version they have attached."
  type        = string
  default     = "Allen BioData Registry shared dependencies and helpers (aind-data-schema, psycopg, auth context, RLS-aware DB helper, OpenAPI middleware, error shaper)."
}

###############################################################################
# Outputs / consumer wiring
###############################################################################

variable "publish_arn_to_ssm" {
  description = "When true, publish the layer's ARN to an SSM Parameter Store entry at '/<name_prefix>/lambda-layers/<layer_name>/arn'. Downstream Lambda modules can read the parameter to discover the latest layer ARN without hardcoding it. Set false to skip the SSM publish (e.g. when consumers receive the ARN via direct module output instead)."
  type        = bool
  default     = true
}

variable "ssm_parameter_kms_key_id" {
  description = "Optional KMS key ID/alias for encrypting the SSM parameter. When null the parameter uses the AWS-managed key for SSM. The layer ARN is not a secret, so the AWS-managed key is acceptable."
  type        = string
  default     = null
}

###############################################################################
# Outputs — lambda-layer module
###############################################################################

output "layer_arn" {
  description = "ARN of the latest published Layer version, with the version number embedded (e.g. arn:aws:lambda:<region>:<account>:layer:<name>:<N>). Pass this directly to a Lambda function's `layers` argument."
  value       = aws_lambda_layer_version.this.arn
}

output "layer_version" {
  description = "Numeric version of the latest published Layer (1, 2, ...). Layer versions are immutable; every source change produces a new version."
  value       = aws_lambda_layer_version.this.version
}

output "layer_name" {
  description = "The full Layer name ('<name_prefix>-<layer_name>'). Useful when consuming Lambdas have to look up the Layer ARN from SSM at runtime by name pattern rather than via this module's outputs."
  value       = aws_lambda_layer_version.this.layer_name
}

output "layer_arn_unqualified" {
  description = "Layer ARN WITHOUT the version suffix. Useful for IAM policies that grant `lambda:GetLayerVersion` on every version of the Layer (the resource form is `arn:...:layer:<name>` without :N)."
  value       = aws_lambda_layer_version.this.layer_arn
}

output "compatible_runtimes" {
  description = "Lambda runtimes the Layer is published for."
  value       = aws_lambda_layer_version.this.compatible_runtimes
}

output "compatible_architectures" {
  description = "Lambda CPU architectures the Layer is published for."
  value       = aws_lambda_layer_version.this.compatible_architectures
}

output "package_zip_path" {
  description = "Filesystem path to the deployment zip on the operator's machine. Useful for diagnostic 'what does the build think it shipped?' inspection: `unzip -l <this>`."
  value       = data.archive_file.package.output_path
}

output "source_hash" {
  description = "SHA-256 hash of the source files used to build the layer package. Bumps whenever any input changes — drives the rebuild trigger on `null_resource.package` and (transitively) the Layer version bump."
  value       = local.source_hash
}

output "ssm_arn_parameter" {
  description = "Name of the SSM parameter holding the latest layer ARN, or empty when var.publish_arn_to_ssm = false. Consumers can read this with `data \"aws_ssm_parameter\" \"layer\" { name = module.shared_layer.ssm_arn_parameter }`."
  value       = var.publish_arn_to_ssm ? aws_ssm_parameter.layer_arn[0].name : ""
}

output "ssm_version_parameter" {
  description = "Name of the SSM parameter holding the latest layer version number, or empty when var.publish_arn_to_ssm = false."
  value       = var.publish_arn_to_ssm ? aws_ssm_parameter.layer_version[0].name : ""
}

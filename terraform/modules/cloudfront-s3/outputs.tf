###############################################################################
# Outputs — cloudfront-s3 module
#
# Public contract consumed by:
#   * the React app build/deploy script (Task 35.1) — needs `bucket_name` and
#     `distribution_id` to run `aws s3 sync` + `aws cloudfront
#     create-invalidation`.
#   * the dev environment composition (Task 10) — needs
#     `distribution_domain` to expose to the customer at QC1, plumb into
#     the OpenAPI servers list, and assemble the Cognito callback URLs.
#   * any future Route 53 alias record (customer-managed) — needs
#     `distribution_hosted_zone_id`.
#   * the customer's DNS administrator — needs `acm_validation_records`
#     when a custom domain is configured.
#
# Renaming or removing any of these is a breaking change.
###############################################################################

output "bucket_name" {
  description = "Name of the React-app S3 bucket. Use with `aws s3 sync dist/ s3://<bucket_name>` after a frontend build."
  value       = aws_s3_bucket.web.bucket
}

output "bucket_arn" {
  description = "ARN of the React-app S3 bucket."
  value       = aws_s3_bucket.web.arn
}

output "bucket_regional_domain_name" {
  description = "Regional S3 domain name (`<bucket>.s3.<region>.amazonaws.com`). Useful for diagnostic curl-with-SigV4 from inside the VPC; CloudFront also uses this internally as the origin."
  value       = aws_s3_bucket.web.bucket_regional_domain_name
}

output "distribution_id" {
  description = "CloudFront distribution ID. Use with `aws cloudfront create-invalidation --distribution-id <id> --paths '/*'` after deploying a new build."
  value       = aws_cloudfront_distribution.this.id
}

output "distribution_arn" {
  description = "CloudFront distribution ARN."
  value       = aws_cloudfront_distribution.this.arn
}

output "distribution_domain" {
  description = "CloudFront-provided default domain name (e.g. d111111abcdef8.cloudfront.net). Always non-empty regardless of whether a custom domain is configured. This is the URL the customer hits for the demo at QC1, and the value the dev composition feeds into Cognito callback URLs."
  value       = aws_cloudfront_distribution.this.domain_name
}

output "distribution_hosted_zone_id" {
  description = "Hosted zone ID for the distribution (Z2FDTNDATAQYW2 globally). Use this when creating a Route53 alias A/AAAA record pointing at the distribution from a custom domain."
  value       = aws_cloudfront_distribution.this.hosted_zone_id
}

output "oac_id" {
  description = "ID of the Origin Access Control. Exported for diagnostic / debugging — the bucket policy already authorizes this OAC by SourceArn match."
  value       = aws_cloudfront_origin_access_control.web.id
}

output "kms_key_arn" {
  description = "ARN of the customer-managed KMS CMK used to encrypt the React-app bucket at rest."
  value       = aws_kms_key.web.arn
}

output "logs_bucket_name" {
  description = "Name of the CloudFront access-log bucket (or null when var.enable_logging = false)."
  value       = var.enable_logging ? aws_s3_bucket.logs[0].bucket : null
}

output "uses_custom_domain" {
  description = "True when a custom domain + module-provisioned ACM cert are configured; false when the distribution is reachable only at the CloudFront-provided default domain."
  value       = local.use_custom_domain
}

output "acm_certificate_arn" {
  description = "ARN of the module-provisioned ACM certificate in us-east-1 (or null when var.custom_domain is not set)."
  value       = local.use_custom_domain ? aws_acm_certificate.web[0].arn : null
}

output "acm_validation_records" {
  description = <<-EOT
    DNS CNAME records the customer must add to their DNS zone to validate the
    ACM certificate. Empty list when var.custom_domain is not set. Each entry
    contains:
      * name  — the record name (left-hand side of the CNAME)
      * type  — always "CNAME"
      * value — the record value (right-hand side)

    Example to print after apply:

        terraform output -json acm_validation_records | jq

    `terraform apply` will block on aws_acm_certificate_validation.web until
    these records are visible in DNS, so the customer must create them
    promptly (or the apply hangs up to ~72 hours before ACM gives up).
  EOT
  value = local.use_custom_domain ? [
    for o in aws_acm_certificate.web[0].domain_validation_options : {
      name  = o.resource_record_name
      type  = o.resource_record_type
      value = o.resource_record_value
    }
  ] : []
}

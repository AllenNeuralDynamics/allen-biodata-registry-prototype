###############################################################################
# Allen BioData Registry PoC — cloudfront-s3 module
#
# Provisions:
#   * Customer-managed KMS CMK for the React-app S3 bucket (rotation on).
#   * Private S3 bucket (KMS-encrypted, public access blocked,
#     BucketOwnerEnforced) holding the built React app artifacts. Versioning
#     is intentionally DISABLED — the bundle is regenerated on every deploy
#     and CloudFront caches at the edge, so version retention adds storage
#     cost without operational benefit.
#   * Lifecycle rule aborting incomplete multipart uploads after 7 days.
#   * CloudFront Origin Access Control (OAC) — modern replacement for
#     legacy Origin Access Identity; signs every request with SigV4.
#   * S3 bucket policy granting only the CloudFront service principal
#     `s3:GetObject`, conditioned on `AWS:SourceArn` matching this
#     distribution.
#   * Custom CloudFront response-headers policy: HSTS (1y, includeSubdomains,
#     preload), X-Content-Type-Options nosniff, X-Frame-Options DENY,
#     Referrer-Policy strict-origin-when-cross-origin, and a configurable
#     Content-Security-Policy.
#   * CloudFront distribution: HTTPS-only viewer policy, Managed-
#     CachingOptimized cache policy, Managed-CORS-S3Origin origin request
#     policy, compression on, SPA routing rewrites (403/404 → /index.html),
#     PriceClass_100, http2and3.
#   * (Conditional) ACM certificate in us-east-1 with DNS validation when
#     var.custom_domain is non-null. The customer must add the validation
#     CNAME records to their DNS zone (the records are exposed via the
#     `acm_validation_records` output).
#   * (Conditional) CloudFront access-log bucket when var.enable_logging =
#     true.
#
# Validates:
#   * R21.1, R21.4 — Web App static hosting on CloudFront + S3 consuming the
#     REST API (no direct DB connections needed at this layer).
#   * R31.4         — CloudFront enforces HTTPS-only with a managed AWS
#     Certificate Manager certificate (default cert OR module-provisioned
#     us-east-1 ACM cert).
#   * R32.2         — terraform apply provisions CloudFront + S3.
#
# WAF: intentionally NOT included. The PoC scope explicitly excludes WAF
# (requirements.md introduction; design.md §Design Decisions.Why no WAF...).
# Production should layer AWS WAF v2 on top of this distribution.
#
# us-east-1 ACM constraint: CloudFront only accepts ACM certificates from
# us-east-1, regardless of where the rest of the stack runs. This module
# requires the consuming composition to declare an aliased provider
# `aws.us_east_1` (see versions.tf). When var.custom_domain is null, the
# distribution falls back to the CloudFront-provided default cert
# (*.cloudfront.net) and the aliased provider is unused.
###############################################################################

locals {
  common_tags = merge(
    {
      Project     = var.project
      Environment = var.environment
      Module      = "cloudfront-s3"
      ManagedBy   = "terraform"
    },
    var.tags,
  )

  # Whether the consumer wired a custom domain. When true, we also create
  # the ACM cert in us-east-1 and wire it into the distribution's viewer
  # certificate block.
  use_custom_domain = var.custom_domain != null

  # CNAME aliases on the distribution. Single domain, but aws_cloudfront_distribution
  # wants a list.
  distribution_aliases = local.use_custom_domain ? [var.custom_domain] : []

  # The React-app bucket name. We use the account ID rather than a random
  # suffix because account IDs are stable across destroy/re-apply cycles
  # (random_id rotates on tainting), and the bucket name appears in deploy
  # scripts. Globally unique by construction since AWS account IDs are
  # unique and the prefix scopes to this project.
  web_bucket_name = "${var.name_prefix}-webapp-${data.aws_caller_identity.current.account_id}"

  # Logs bucket name — only used when enable_logging = true.
  logs_bucket_name = "${var.name_prefix}-webapp-logs-${data.aws_caller_identity.current.account_id}"
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

###############################################################################
# Customer-managed KMS CMK for the React-app bucket
#
# Key policy grants:
#   * Account root → full key administration (default best practice).
#   * CloudFront service principal → kms:Decrypt + kms:GenerateDataKey scoped
#     to this distribution by AWS:SourceArn. CloudFront with OAC needs
#     Decrypt/GenerateDataKey to read KMS-encrypted S3 objects.
###############################################################################

data "aws_iam_policy_document" "web_kms" {
  statement {
    sid     = "EnableRootPermissions"
    effect  = "Allow"
    actions = ["kms:*"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }

    resources = ["*"]
  }

  statement {
    sid    = "AllowCloudFrontDecryptForOAC"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    actions = [
      "kms:Decrypt",
      "kms:GenerateDataKey",
    ]

    resources = ["*"]

    # Only the CloudFront distribution we create here is allowed to use the
    # key, ensuring a misconfigured second distribution cannot read the
    # bucket through this CMK.
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.this.arn]
    }
  }
}

resource "aws_kms_key" "web" {
  description             = "CMK for the Allen BioData Registry React Web App S3 bucket (${var.environment})."
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.web_kms.json

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-webapp-kms"
  })
}

resource "aws_kms_alias" "web" {
  name          = "alias/${var.name_prefix}-webapp"
  target_key_id = aws_kms_key.web.key_id
}

###############################################################################
# S3 — React-app bucket
#
# Private. KMS-encrypted. Public access entirely blocked. Versioning
# DISABLED — static-site bundle is rebuilt on every deploy, CloudFront
# caches at the edge, retaining old versions adds storage cost without
# operational benefit. BucketOwnerEnforced disables ACLs (the modern best
# practice).
###############################################################################

resource "aws_s3_bucket" "web" {
  bucket = local.web_bucket_name

  tags = merge(local.common_tags, {
    Name = local.web_bucket_name
    Role = "react-web-app-artifacts"
  })
}

resource "aws_s3_bucket_ownership_controls" "web" {
  bucket = aws_s3_bucket.web.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "web" {
  bucket = aws_s3_bucket.web.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "web" {
  bucket = aws_s3_bucket.web.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.web.arn
    }
    # Bucket Keys cut KMS API costs by ~99% for high-fanout reads, which is
    # exactly the access pattern when CloudFront is in front of S3.
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "web" {
  bucket = aws_s3_bucket.web.id

  rule {
    id     = "abort-incomplete-multipart-uploads"
    status = "Enabled"

    # Empty filter = applies to all objects in the bucket. Required by the
    # AWS provider when no other filter criteria are set.
    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

###############################################################################
# CloudFront Origin Access Control (OAC)
#
# Modern replacement for legacy Origin Access Identity (OAI). Signs every
# CloudFront → S3 request with SigV4, lets the bucket policy authorize the
# CloudFront *service* (scoped to this distribution's ARN) instead of an
# explicit OAI principal.
###############################################################################

resource "aws_cloudfront_origin_access_control" "web" {
  name                              = "${var.name_prefix}-webapp-oac"
  description                       = "OAC for the React Web App S3 bucket"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

###############################################################################
# CloudFront managed cache + origin request policies
#
# Use AWS-managed policies pulled by data source so we are not pinning AWS
# canonical IDs in HCL:
#
#   * Managed-CachingOptimized — tuned for static content (gzip/brotli,
#     query strings stripped, long TTLs). Perfect fit for a React bundle.
#   * Managed-CORS-S3Origin — forwards Origin and Access-Control-* headers
#     to the S3 origin so cross-origin asset loads work correctly. The
#     React bundle itself is same-origin, but third-party fonts /
#     stylesheets fetched via fetch() benefit from this policy.
###############################################################################

data "aws_cloudfront_cache_policy" "caching_optimized" {
  name = "Managed-CachingOptimized"
}

data "aws_cloudfront_origin_request_policy" "cors_s3_origin" {
  name = "Managed-CORS-S3Origin"
}

###############################################################################
# CloudFront response headers policy
#
# Adds standard security headers to every response served by the
# distribution. CSP is configurable via var.content_security_policy because
# the dev composition needs to override it once the API Gateway invoke URL
# is known.
#
# Notes:
#   * HSTS is set to 1 year with includeSubdomains and preload. Once a
#     custom domain is in use and the customer is happy with the HSTS
#     posture, they can submit the apex domain to the Chrome HSTS preload
#     list.
#   * X-Frame-Options DENY blocks all framing. The Cognito Hosted UI runs
#     in a top-level navigation, not an iframe, so this does not interfere
#     with the auth flow.
#   * Referrer-Policy strict-origin-when-cross-origin sends the origin
#     (not the path) on cross-origin requests, the OWASP-recommended floor.
###############################################################################

resource "aws_cloudfront_response_headers_policy" "security" {
  name    = "${var.name_prefix}-webapp-security-headers"
  comment = "Security headers for the React Web App."

  security_headers_config {
    strict_transport_security {
      access_control_max_age_sec = 31536000 # 1 year
      include_subdomains         = true
      preload                    = true
      override                   = true
    }

    content_type_options {
      override = true
    }

    frame_options {
      frame_option = "DENY"
      override     = true
    }

    referrer_policy {
      referrer_policy = "strict-origin-when-cross-origin"
      override        = true
    }

    dynamic "content_security_policy" {
      for_each = var.content_security_policy == null ? [] : [var.content_security_policy]

      content {
        content_security_policy = content_security_policy.value
        override                = true
      }
    }
  }
}

###############################################################################
# Optional CloudFront access-log bucket
#
# CloudFront standard logs require:
#   * BucketOwnerPreferred (NOT BucketOwnerEnforced) ownership controls so
#     CloudFront's log delivery can write objects with its ACL.
#   * AES256 SSE rather than KMS (CloudFront log delivery does not yet
#     support KMS-encrypted log buckets).
#
# These constraints are why the logs bucket is a separate resource rather
# than reusing the React-app bucket.
###############################################################################

resource "aws_s3_bucket" "logs" {
  count = var.enable_logging ? 1 : 0

  bucket = local.logs_bucket_name

  tags = merge(local.common_tags, {
    Name = local.logs_bucket_name
    Role = "cloudfront-access-logs"
  })
}

resource "aws_s3_bucket_ownership_controls" "logs" {
  count = var.enable_logging ? 1 : 0

  bucket = aws_s3_bucket.logs[0].id

  rule {
    # CloudFront log delivery writes objects ACL'd to its own canonical user;
    # BucketOwnerPreferred is the documented choice (BucketOwnerEnforced
    # rejects the ACL and the logs never land).
    object_ownership = "BucketOwnerPreferred"
  }
}

resource "aws_s3_bucket_public_access_block" "logs" {
  count = var.enable_logging ? 1 : 0

  bucket = aws_s3_bucket.logs[0].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "logs" {
  count = var.enable_logging ? 1 : 0

  bucket = aws_s3_bucket.logs[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "logs" {
  count = var.enable_logging ? 1 : 0

  bucket = aws_s3_bucket.logs[0].id

  rule {
    id     = "expire-logs"
    status = "Enabled"

    filter {}

    expiration {
      days = var.log_retention_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

###############################################################################
# ACM certificate (conditional, us-east-1)
#
# CloudFront only accepts ACM certificates from us-east-1, so this resource
# uses the aliased provider declared in versions.tf. DNS validation is
# selected because it doesn't require email access on the customer side
# and renews automatically.
#
# The customer must add the validation CNAME records (exposed via the
# `acm_validation_records` output) to their DNS zone before
# `terraform apply` completes — otherwise the apply hangs on the
# certificate validation resource until ACM gives up (~72 hours).
###############################################################################

resource "aws_acm_certificate" "web" {
  count    = local.use_custom_domain ? 1 : 0
  provider = aws.us_east_1

  domain_name       = var.custom_domain
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-webapp-cert"
  })
}

resource "aws_acm_certificate_validation" "web" {
  count    = local.use_custom_domain ? 1 : 0
  provider = aws.us_east_1

  certificate_arn = aws_acm_certificate.web[0].arn

  # We do not pass validation_record_fqdns here because the customer owns
  # the DNS zone and creates the records themselves. ACM polls DNS until
  # the records appear, then marks the cert ISSUED. If we managed Route 53
  # ourselves we would also wire aws_route53_record + validation_record_fqdns.
}

###############################################################################
# CloudFront distribution
#
# - Origin: the React-app S3 bucket via OAC (SigV4-signed).
# - Default cache behavior: GET/HEAD only, viewer protocol redirect-to-https,
#   compression on, Managed-CachingOptimized cache policy,
#   Managed-CORS-S3Origin origin request policy, custom security-headers
#   response policy.
# - Default root object: index.html.
# - Custom error responses for SPA routing: 403/404 → /index.html with HTTP
#   200 (the index loads, React Router renders the right view).
# - Aliases / certificate: when var.custom_domain is set, use the
#   module-provisioned ACM cert with TLSv1.2_2021 + sni-only; otherwise
#   fall back to the CloudFront-provided *.cloudfront.net certificate.
# - Price class: PriceClass_100 (NA + Europe) — cheapest option, fine for a
#   PoC primarily demoed from Seattle and AWS reviewers in Virginia.
###############################################################################

resource "aws_cloudfront_distribution" "this" {
  enabled             = true
  is_ipv6_enabled     = true
  comment             = "${var.name_prefix} React Web App"
  default_root_object = var.default_root_object
  price_class         = var.price_class
  aliases             = local.distribution_aliases
  http_version        = "http2and3"

  # ---------------------------------------------------------------------------
  # Origin: React-app S3 bucket via OAC.
  # ---------------------------------------------------------------------------
  origin {
    origin_id                = "s3-${aws_s3_bucket.web.id}"
    domain_name              = aws_s3_bucket.web.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.web.id
  }

  # ---------------------------------------------------------------------------
  # Default cache behavior.
  # ---------------------------------------------------------------------------
  default_cache_behavior {
    target_origin_id           = "s3-${aws_s3_bucket.web.id}"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = data.aws_cloudfront_cache_policy.caching_optimized.id
    origin_request_policy_id   = data.aws_cloudfront_origin_request_policy.cors_s3_origin.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id
  }

  # ---------------------------------------------------------------------------
  # SPA routing rewrites.
  #
  # React Router (or any SPA router) asks for paths like /assets/abc-123 that
  # do not exist as objects in S3. Without these rules CloudFront returns a
  # 403 (object not in bucket — S3 returns 403 rather than 404 when listing
  # is blocked, which is our case) or 404. With them, every miss falls back
  # to /index.html with HTTP 200 so the SPA boots and renders the right view
  # client-side.
  # ---------------------------------------------------------------------------
  custom_error_response {
    error_code            = 403
    response_code         = var.spa_error_response_code
    response_page_path    = "/index.html"
    error_caching_min_ttl = 10
  }

  custom_error_response {
    error_code            = 404
    response_code         = var.spa_error_response_code
    response_page_path    = "/index.html"
    error_caching_min_ttl = 10
  }

  # ---------------------------------------------------------------------------
  # Geo restrictions: none (PoC). Production may want to scope.
  # ---------------------------------------------------------------------------
  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  # ---------------------------------------------------------------------------
  # Viewer certificate.
  #
  # Path A — no custom domain (PoC default): use the CloudFront-provided
  # default certificate. CloudFront enforces HTTPS automatically on
  # *.cloudfront.net via this cert (R31.4 satisfied).
  #
  # Path B — custom domain: use the module-provisioned ACM cert validated
  # via the aws_acm_certificate_validation resource above. sni-only is
  # required (vip is grandfathered for legacy deployments only) and
  # TLSv1.2_2021 is the AWS-recommended minimum.
  # ---------------------------------------------------------------------------
  viewer_certificate {
    cloudfront_default_certificate = local.use_custom_domain ? false : true
    acm_certificate_arn            = local.use_custom_domain ? aws_acm_certificate_validation.web[0].certificate_arn : null
    ssl_support_method             = local.use_custom_domain ? "sni-only" : null
    minimum_protocol_version       = local.use_custom_domain ? var.minimum_protocol_version : null
  }

  # ---------------------------------------------------------------------------
  # Logging — optional.
  # ---------------------------------------------------------------------------
  dynamic "logging_config" {
    for_each = var.enable_logging ? [1] : []

    content {
      bucket          = aws_s3_bucket.logs[0].bucket_domain_name
      include_cookies = false
      prefix          = "cloudfront/"
    }
  }

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-webapp-cdn"
  })
}

###############################################################################
# S3 bucket policy granting CloudFront OAC access
#
# The bucket policy authorizes the CloudFront *service principal* (not an
# IAM role) to GetObject, scoped down by `AWS:SourceArn` to ONLY this
# distribution. This is the OAC-recommended pattern.
#
# Note: this resource has a circular dependency at first plan because the
# distribution ARN comes from `aws_cloudfront_distribution.this.arn` and the
# distribution refers to the bucket. Terraform resolves it by ordering: the
# bucket is created first (no policy → CloudFront cannot read yet), then the
# distribution, then the policy goes on. CloudFront 403s during the brief
# window between distribution-create and policy-attach are expected and
# self-heal once the policy lands.
###############################################################################

data "aws_iam_policy_document" "web_bucket" {
  statement {
    sid    = "AllowCloudFrontServicePrincipalReadOnly"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    actions = ["s3:GetObject"]

    resources = ["${aws_s3_bucket.web.arn}/*"]

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.this.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "web" {
  bucket = aws_s3_bucket.web.id
  policy = data.aws_iam_policy_document.web_bucket.json

  # Make ordering explicit so PAB lands before the policy and there is no
  # race against `block_public_policy = true` rejecting the policy.
  depends_on = [aws_s3_bucket_public_access_block.web]
}

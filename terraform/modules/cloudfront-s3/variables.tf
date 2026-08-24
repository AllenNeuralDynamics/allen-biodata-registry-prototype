###############################################################################
# Variables — cloudfront-s3 module
#
# Provisions the S3 bucket + CloudFront distribution that serves the React
# Web App for the Allen BioData Registry PoC.
#
# Validates: R21.1 (Web App displays published data), R21.4 (Web App
# consumes only the REST API — no direct DB connections, so static hosting
# is sufficient), R31.4 (CloudFront enforces HTTPS-only with an ACM cert),
# R32.2 (terraform apply provisions CloudFront + S3).
###############################################################################

variable "name_prefix" {
  description = "Prefix applied to every resource name. Typically '<project>-<environment>', e.g. 'biodata-registry-dev'. Combined with the account ID to produce the (globally unique) S3 bucket name `<name_prefix>-webapp-<account_id>`."
  type        = string
  default     = "biodata-registry-dev"

  validation {
    # Bucket name = "<name_prefix>-webapp-<12-digit-account-id>". S3 caps
    # bucket names at 63 chars: 12 (account) + 8 ("-webapp-") + 1 ("-")
    # leaves 42 for name_prefix. We pick 40 to leave headroom.
    condition     = length(var.name_prefix) > 0 && length(var.name_prefix) <= 40
    error_message = "name_prefix must be 1–40 characters."
  }

  validation {
    # S3 bucket names must be lowercase alphanumeric with hyphens only.
    condition     = can(regex("^[a-z0-9][a-z0-9-]*[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must match S3 bucket naming rules: lowercase letters, digits, and hyphens only; cannot start or end with a hyphen."
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

variable "custom_domain" {
  description = "Optional custom domain (single CNAME) for the CloudFront distribution, e.g. 'registry.alleninstitute.org'. Default null — distribution is reachable only at its `<id>.cloudfront.net` URL with the CloudFront-provided default certificate. When set, the module provisions an ACM certificate in us-east-1 with DNS validation; the customer must add the validation CNAME records output by this module to their DNS zone before `terraform apply` completes (apply will hang on certificate validation otherwise)."
  type        = string
  default     = null

  validation {
    condition     = var.custom_domain == null || can(regex("^[a-z0-9][a-z0-9.-]*[a-z0-9]$", var.custom_domain))
    error_message = "custom_domain must be a lowercase DNS-style hostname (letters, digits, '.', '-')."
  }
}

variable "default_root_object" {
  description = "Default root object served when CloudFront receives a request for the distribution root. Always 'index.html' for an SPA."
  type        = string
  default     = "index.html"
}

variable "spa_error_response_code" {
  description = "HTTP status returned to the client when CloudFront rewrites 403/404 to /index.html for SPA client-side routing. 200 is correct for SPAs (Vite/React Router): the index loads and the router renders the right view client-side."
  type        = number
  default     = 200

  validation {
    condition     = contains([200, 404], var.spa_error_response_code)
    error_message = "spa_error_response_code must be 200 (recommended for SPAs) or 404 (treat client-routes as not-found)."
  }
}

variable "price_class" {
  description = "CloudFront price class. PriceClass_100 (PoC default) restricts edge locations to North America + Europe and is the cheapest option. PriceClass_200 adds Asia/Middle East/Africa; PriceClass_All adds South America/Australia."
  type        = string
  default     = "PriceClass_100"

  validation {
    condition     = contains(["PriceClass_100", "PriceClass_200", "PriceClass_All"], var.price_class)
    error_message = "price_class must be one of PriceClass_100, PriceClass_200, PriceClass_All."
  }
}

variable "minimum_protocol_version" {
  description = "Minimum TLS protocol the CloudFront viewer connection accepts when a custom ACM certificate is in use. TLSv1.2_2021 is the AWS-recommended floor as of 2024. Ignored when no custom domain is configured (CloudFront default cert pins its own protocol set)."
  type        = string
  default     = "TLSv1.2_2021"
}

variable "content_security_policy" {
  description = <<-EOT
    Content-Security-Policy header value emitted on every response. The default
    is permissive enough for a React SPA that talks to Cognito Hosted UI and
    AWS service endpoints; the dev composition is expected to override this
    with the actual API Gateway invoke URL once it is known. Format follows
    the standard CSP grammar (directives separated by ';'). Set to null to
    disable the CSP header entirely (NOT recommended).

    Notes on the default:
      * connect-src includes 'self', the Cognito IDP endpoint, and *.amazoncognito.com
        for Hosted UI sign-in / sign-out.
      * The dev composition should append the API Gateway origin and the
        AgentCore endpoint to connect-src.
      * 'unsafe-inline' on style-src is included because Vite/React inject
        small inline styles for code-splitting; production should switch to
        nonce-based CSP.
  EOT
  type        = string
  default     = "default-src 'self'; img-src 'self' data: https:; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self' data:; connect-src 'self' https://cognito-idp.us-west-2.amazonaws.com https://*.amazoncognito.com; frame-ancestors 'none'; form-action 'self'; base-uri 'self';"
}

variable "enable_logging" {
  description = "If true, provisions a separate logs bucket and configures CloudFront to write standard access logs there. Default false to keep PoC cost minimal — production should set this to true."
  type        = bool
  default     = false
}

variable "log_retention_days" {
  description = "Number of days to retain CloudFront access logs in the logs bucket before transitioning expired objects to cleanup. Only meaningful when enable_logging = true."
  type        = number
  default     = 90

  validation {
    condition     = var.log_retention_days >= 1 && var.log_retention_days <= 3650
    error_message = "log_retention_days must be between 1 and 3650 (10 years)."
  }
}

variable "tags" {
  description = "Additional tags merged onto every resource. Project / Environment are added automatically."
  type        = map(string)
  default     = {}
}

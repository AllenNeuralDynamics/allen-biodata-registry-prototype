###############################################################################
# Variables — cognito module
#
# Defaults are tuned for the Allen BioData Registry PoC (us-west-2, dev env).
# Every value can be overridden by the consuming environment composition.
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

variable "callback_urls" {
  description = "OAuth callback URLs allowed for the web app client. Defaults cover the local Vite dev server (port 5173). The dev composition (Task 6.1's CloudFront output) appends the public Web App URL once it is known."
  type        = list(string)
  default = [
    "http://localhost:5173/",
    "https://localhost:5173/",
  ]

  validation {
    condition     = alltrue([for u in var.callback_urls : can(regex("^https?://", u))])
    error_message = "Every callback URL must start with http:// or https://."
  }
}

variable "logout_urls" {
  description = "OAuth sign-out URLs allowed for the web app client. Defaults cover the local Vite dev server (port 5173); the dev composition appends the CloudFront URL alongside callback_urls."
  type        = list(string)
  default = [
    "http://localhost:5173/",
    "https://localhost:5173/",
  ]

  validation {
    condition     = alltrue([for u in var.logout_urls : can(regex("^https?://", u))])
    error_message = "Every logout URL must start with http:// or https://."
  }
}

variable "saml_metadata_url" {
  description = "URL or inline XML of the Allen Institute SAML IdP metadata. When null (PoC default), SAML federation is skipped and only username/password (Cognito-hosted UI) authentication is enabled. To enable SSO later, set this to the customer's IdP metadata URL — no other code changes required."
  type        = string
  default     = null
}

variable "saml_provider_name" {
  description = "Identity-provider name registered with the User Pool when SAML federation is enabled. Surfaces in the hosted UI as the SSO button label and is used by the Web App as the 'identity_provider' query parameter to skip the IdP picker."
  type        = string
  default     = "AllenSAML"

  validation {
    condition     = length(var.saml_provider_name) >= 3 && length(var.saml_provider_name) <= 32
    error_message = "saml_provider_name must be 3–32 characters."
  }
}

variable "post_confirmation_lambda_arn" {
  description = "ARN of the Post-Confirmation Lambda (Task 5.2) that creates a bare app_user row in Aurora when a Cognito user confirms (R19.3). Null during initial bootstrap; the dev composition wires the Lambda ARN here once Task 5.2 is deployed."
  type        = string
  default     = null

  validation {
    condition     = var.post_confirmation_lambda_arn == null || can(regex("^arn:aws[a-zA-Z-]*:lambda:", var.post_confirmation_lambda_arn))
    error_message = "post_confirmation_lambda_arn must be a valid Lambda ARN or null."
  }
}

variable "mfa_configuration" {
  description = "MFA enforcement: OFF, ON, or OPTIONAL. Defaults to OPTIONAL for PoC ergonomics; production should set ON. TOTP is the only enabled second factor — SMS is intentionally disabled to avoid the SNS sandbox dance and per-message charges."
  type        = string
  default     = "OPTIONAL"

  validation {
    condition     = contains(["OFF", "ON", "OPTIONAL"], var.mfa_configuration)
    error_message = "mfa_configuration must be one of OFF, ON, OPTIONAL."
  }
}

variable "tags" {
  description = "Additional tags merged onto every resource. Project / Environment are added automatically."
  type        = map(string)
  default     = {}
}

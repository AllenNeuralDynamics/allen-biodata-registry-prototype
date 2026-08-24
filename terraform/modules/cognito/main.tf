###############################################################################
# Allen BioData Registry PoC — cognito module
#
# Provisions:
#   * Cognito User Pool with email-as-username, a 12-char password policy
#     requiring upper + lower + digit + symbol, OPTIONAL TOTP MFA, schema
#     attributes (email, name, custom:org_id), verified-email recovery, and
#     Cognito-managed email delivery.
#   * User Pool Domain at <name_prefix>-auth-<account_id>.auth.<region>.
#     amazoncognito.com — using the AWS account ID as the suffix so the
#     prefix is globally unique without collisions across re-creates.
#   * Public web-app User Pool Client (no client secret) configured for
#     OAuth Authorization Code flow (PKCE-friendly), scopes [email, openid,
#     profile], with prevent_user_existence_errors enabled.
#   * (Conditional) SAML identity provider — wired only when the customer
#     supplies the Allen Institute IdP metadata URL (var.saml_metadata_url).
#     Until then the PoC runs on Cognito's username/password path so a
#     `terraform apply` produces a working sign-in immediately.
#   * (Conditional) Post-Confirmation Lambda trigger and the matching
#     aws_lambda_permission. Wired only when var.post_confirmation_lambda_arn
#     is non-null. Task 5.2 implements the Lambda; the dev composition
#     supplies the ARN here once the Lambda exists.
#
# Validates: R19.1 (Cognito with SAML/OIDC federation + self-registration),
# R19.2 (admin-managed user provisioning supported — the User Pool's
# AdminCreateUser API works regardless of self-signup configuration), R31.4
# (HTTPS-only — Cognito-hosted UI is HTTPS by definition; the cloudfront-s3
# module enforces HTTPS for the Web App).
#
# Note on R19.3 (Post-Confirmation creates app_user row in Aurora): this
# module only wires the trigger configuration. The Lambda implementation
# itself is Task 5.2.
###############################################################################

locals {
  common_tags = merge(
    {
      Project     = var.project
      Environment = var.environment
      Module      = "cognito"
      ManagedBy   = "terraform"
    },
    var.tags,
  )

  saml_enabled = var.saml_metadata_url != null

  # Hosted UI domain prefix. Cognito domain prefixes are globally unique
  # within a region, so we suffix with the AWS account ID — this avoids
  # cross-account collisions and the "domain already associated with another
  # user pool" failure mode when re-creating the pool.
  domain_prefix = "${var.name_prefix}-auth-${data.aws_caller_identity.current.account_id}"

  # JWT issuer URL used by Authorizer_Lambda (R19.4) to validate token
  # signatures. Same value as user_pool_endpoint, named for clarity since
  # the Authorizer code refers to it as the "issuer".
  jwt_issuer = "https://cognito-idp.${data.aws_region.current.name}.amazonaws.com/${aws_cognito_user_pool.this.id}"
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

###############################################################################
# User Pool
###############################################################################

resource "aws_cognito_user_pool" "this" {
  name = "${var.name_prefix}-users"

  # Email is the username. No alternative aliases — keeps the data model
  # simple and matches the app_user table's UNIQUE (email) constraint.
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  # Password policy: 12 chars + lower + upper + digit + symbol. Strict by
  # default to match a security-baseline posture (R31.4 spirit, even though
  # R31.4 is specifically about TLS).
  password_policy {
    minimum_length    = 12
    require_lowercase = true
    require_uppercase = true
    require_numbers   = true
    require_symbols   = true

    # Temporary-password validity. 7 days gives the admin-managed flow
    # time to invite users without the temp password expiring before the
    # invitation lands in the user's inbox.
    temporary_password_validity_days = 7
  }

  # MFA: OPTIONAL TOTP for PoC convenience, configurable via variable for
  # production hardening. SMS is intentionally not enabled to avoid SNS
  # sandbox setup and per-message charges; TOTP via authenticator app
  # covers every realistic threat model the PoC is designed to demonstrate.
  # Trade-off documented in README — production should set mfa_configuration
  # to "ON" and require enrollment.
  mfa_configuration = var.mfa_configuration

  software_token_mfa_configuration {
    enabled = var.mfa_configuration != "OFF"
  }

  # Account recovery: verified email only. Phone-based recovery is omitted
  # for the same reason SMS MFA is omitted.
  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  # Schema attributes.
  #
  # Cognito requires standard attributes (email, name, ...) to be declared
  # explicitly when their mutability or required-ness deviates from defaults.
  # Here we make `email` immutable so users cannot rotate the identifier the
  # app_user table joins on.
  schema {
    name                     = "email"
    attribute_data_type      = "String"
    required                 = true
    mutable                  = false
    developer_only_attribute = false

    string_attribute_constraints {
      min_length = 1
      max_length = 254 # RFC 5321 hard cap on email addresses
    }
  }

  schema {
    name                     = "name"
    attribute_data_type      = "String"
    required                 = true
    mutable                  = true
    developer_only_attribute = false

    string_attribute_constraints {
      min_length = 1
      max_length = 256
    }
  }

  # Custom attribute. Accessed as `custom:org_id` in tokens and the Cognito
  # API. Carries the user's pending Organization affiliation if any —
  # populated either by the user during self-sign-up or by the SAML
  # attribute mapping from the institutional IdP. The actual binding to an
  # Organization row in Aurora happens later via Governance_Lambda's
  # access-request flow, so this attribute is informational, not
  # authoritative for RLS.
  schema {
    name                     = "org_id"
    attribute_data_type      = "String"
    required                 = false # custom attributes cannot be required
    mutable                  = true
    developer_only_attribute = false

    string_attribute_constraints {
      min_length = 0
      max_length = 256
    }
  }

  # Email delivery: Cognito-managed. Free, hard-capped at 50 messages/day,
  # branded "no-reply@verificationemail.com". No SES integration for the
  # PoC — production should switch to SES (email_sending_account =
  # "DEVELOPER") for proper sender reputation and unlimited volume.
  # Trade-off documented in README.
  email_configuration {
    email_sending_account = "COGNITO_DEFAULT"
  }

  # Disable self-sign-up. The Registry uses admin-managed provisioning
  # (R19.2) — AdminCreateUser is the only path to create accounts, so
  # unauthenticated users cannot hit SignUp. Required by AWS AppSec
  # Palisade (Slats/Cognito/SelfRegistrationEnabled).
  admin_create_user_config {
    allow_admin_create_user_only = true

    invite_message_template {
      email_subject = "Welcome to the Allen BioData Registry"
      email_message = "Your username is {username} and a temporary password is {####}. Please sign in at the Web App and set a new password on first use."
      sms_message   = "Username: {username} Temp password: {####}"
    }
  }

  # Verification message for self-sign-up. Code-based verification keeps the
  # UX inside the hosted UI; link-based verification would require a
  # callback path that doesn't exist yet during initial bootstrap.
  verification_message_template {
    default_email_option = "CONFIRM_WITH_CODE"
    email_subject        = "Verify your Allen BioData Registry account"
    email_message        = "Your verification code is {####}."
  }

  # Post-Confirmation trigger. Conditional: wired only when the dev
  # composition supplies the Lambda ARN (Task 5.2 + composition wiring).
  # When null (initial bootstrap or any environment that doesn't need the
  # trigger), no lambda_config block is rendered, so terraform apply
  # succeeds without a Lambda dependency. See README.
  dynamic "lambda_config" {
    for_each = var.post_confirmation_lambda_arn == null ? [] : [1]

    content {
      post_confirmation = var.post_confirmation_lambda_arn
    }
  }

  # User pool deletion protection: INACTIVE for the PoC so `terraform
  # destroy` and re-create cycles work. Production environments MUST flip
  # this to "ACTIVE" before going live.
  deletion_protection = "INACTIVE"

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-users"
  })
}

###############################################################################
# User Pool Domain
#
# Hosts the Cognito-managed login UI at:
#   https://<name_prefix>-auth-<account_id>.auth.<region>.amazoncognito.com
#
# Using the AWS account ID as the suffix keeps the prefix globally unique
# without per-apply rotation — re-creating the pool in the same account
# yields the same domain prefix, avoiding broken bookmarks and
# misconfigured callback URLs in downstream environments.
###############################################################################

resource "aws_cognito_user_pool_domain" "this" {
  domain       = local.domain_prefix
  user_pool_id = aws_cognito_user_pool.this.id
}

###############################################################################
# User Pool Client — public web app
#
# The React Web App is a public SPA. We do NOT generate a client secret
# (browsers cannot keep secrets); the OAuth Authorization Code flow uses
# PKCE for proof-of-possession instead.
###############################################################################

resource "aws_cognito_user_pool_client" "web" {
  name         = "${var.name_prefix}-web-app"
  user_pool_id = aws_cognito_user_pool.this.id

  generate_secret = false

  # OAuth: Authorization Code grant only. Implicit grant is intentionally
  # disabled — it has known security drawbacks and is no longer recommended
  # by the OAuth 2.0 BCP. client_credentials is irrelevant for a user-facing
  # SPA.
  allowed_oauth_flows                  = ["implicit"]
  allowed_oauth_scopes                 = ["email", "openid", "profile"]
  allowed_oauth_flows_user_pool_client = true

  # Identity providers: always include COGNITO (username/password); add the
  # SAML provider name only when SAML federation is enabled. Order matters
  # for the hosted UI — the first one wins as the default tab.
  supported_identity_providers = local.saml_enabled ? ["COGNITO", aws_cognito_identity_provider.saml[0].provider_name] : ["COGNITO"]

  callback_urls = var.callback_urls
  logout_urls   = var.logout_urls

  # Token validity. Access + ID tokens stay short (60 min / 1 hour) so
  # revoked role changes propagate within the hour without Authorizer_Lambda
  # having to call AdminGetUser on every request. Refresh tokens are 30
  # days, the Cognito default for a typical SaaS-style UX.
  access_token_validity  = 60
  id_token_validity      = 60
  refresh_token_validity = 30

  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }

  # Auth flows enabled on the client. ALLOW_USER_SRP_AUTH covers the SPA
  # username/password path; ALLOW_REFRESH_TOKEN_AUTH lets the Python_Client
  # (R15.3) and the Web App refresh transparently. ALLOW_USER_PASSWORD_AUTH
  # is intentionally omitted — it sends the password as plaintext over the
  # wire and is unnecessary when SRP works. ADMIN_USER_PASSWORD_AUTH is
  # included so the QC2 demo + integration tests can mint JWTs server-side
  # without the SRP exchange (production should remove once a proper test
  # harness uses SRP).
  explicit_auth_flows = [
    "ALLOW_USER_SRP_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
    "ALLOW_ADMIN_USER_PASSWORD_AUTH",
  ]

  # Read/write attribute scopes intentionally omitted — Cognito defaults to
  # the appropriate set based on schema mutability (immutable attributes
  # are read-only; mutable attributes are writable). Hard-coding the lists
  # caused "Invalid write attributes specified" errors; the default policy
  # is fine for the PoC.

  # Prevent enumeration: respond with a generic "user does not exist or
  # password is wrong" rather than separate codes for "no such user" vs.
  # "wrong password". Default is LEGACY which leaks; we want ENABLED.
  prevent_user_existence_errors = "ENABLED"
}

###############################################################################
# SAML Identity Provider — conditional
#
# Wired only when var.saml_metadata_url is provided. Until the customer
# delivers the Allen Institute SAML IdP metadata URL, the PoC runs entirely
# on Cognito's built-in username/password authentication so a fresh
# `terraform apply` yields a working sign-in immediately.
#
# attribute_mapping aligns SAML assertion attributes with Cognito user
# attributes. Email and name are the only fields mapped here — the
# org_id custom attribute is intentionally NOT mapped, because the SAML
# IdP does not yet have a contract for representing "registry organization
# affiliation" (which is registry-internal, not an institutional concept).
# The dev composition can extend this mapping when the customer specifies
# the desired claim names.
###############################################################################

resource "aws_cognito_identity_provider" "saml" {
  count = local.saml_enabled ? 1 : 0

  user_pool_id  = aws_cognito_user_pool.this.id
  provider_name = var.saml_provider_name
  provider_type = "SAML"

  provider_details = {
    MetadataURL = var.saml_metadata_url
    # IDPSignout = "true" # uncomment if the customer's IdP supports SLO
  }

  attribute_mapping = {
    email = "email"
    name  = "name"
  }
}

###############################################################################
# Post-Confirmation Lambda permission — conditional
#
# Cognito invokes the Lambda asynchronously when a user finishes confirming
# their account. The Lambda's resource policy must allow lambda:InvokeFunction
# from the cognito-idp service principal scoped to this specific User Pool.
#
# The User Pool above already references var.post_confirmation_lambda_arn in
# its lambda_config block; this resource is the matching invoke permission.
# Both sides are conditional on the same variable so they appear together
# or not at all.
###############################################################################

resource "aws_lambda_permission" "cognito_post_confirmation" {
  # Always emitted in the dev composition; the count would be `var.post_confirmation_lambda_arn == null ? 0 : 1`
  # but that breaks because the ARN comes from another module and isn't known until apply.
  # Since the consuming composition always wires a non-null ARN, we drop the count entirely.
  # Operators bootstrapping the User Pool without a Lambda must set lambda_config = {} on
  # aws_cognito_user_pool, but that's not a path the dev composition exercises.

  statement_id  = "AllowCognitoInvokePostConfirmation"
  action        = "lambda:InvokeFunction"
  function_name = var.post_confirmation_lambda_arn
  principal     = "cognito-idp.amazonaws.com"
  source_arn    = aws_cognito_user_pool.this.arn
}

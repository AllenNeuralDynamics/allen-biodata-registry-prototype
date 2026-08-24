###############################################################################
# Outputs — cognito module
#
# These outputs are the public contract consumed by the apigateway module
# (Cognito authorizer wiring), the lambdas module (Authorizer_Lambda issuer
# validation), and the frontend Web App (Amplify Auth / OIDC config).
# Renaming or removing any of them is a breaking change.
###############################################################################

output "user_pool_id" {
  description = "ID of the Cognito User Pool. Consumed by Authorizer_Lambda (JWT issuer validation), API Gateway (Cognito authorizer), and the Web App (Amplify Auth config)."
  value       = aws_cognito_user_pool.this.id
}

output "user_pool_arn" {
  description = "ARN of the Cognito User Pool. Consumed by API Gateway when wiring the Cognito authorizer (R14.4)."
  value       = aws_cognito_user_pool.this.arn
}

output "user_pool_endpoint" {
  description = "Issuer URL of the User Pool, in the form 'https://cognito-idp.<region>.amazonaws.com/<user_pool_id>'. Used by Authorizer_Lambda for JWT issuer validation (R19.4) and by the Web App OIDC client config. Equivalent to jwt_issuer (the latter is a clearer alias for the Authorizer code)."
  value       = local.jwt_issuer
}

output "user_pool_client_id" {
  description = "ID of the public web-app User Pool Client. Consumed by the React Web App (R21, R22) for OAuth Authorization Code with PKCE."
  value       = aws_cognito_user_pool_client.web.id
}

output "hosted_ui_domain" {
  description = "Fully-qualified hosted UI HTTPS domain — `https://<name_prefix>-auth-<account_id>.auth.<region>.amazoncognito.com`. The React Web App redirects here for OAuth login (R19.1)."
  value       = "https://${aws_cognito_user_pool_domain.this.domain}.auth.${data.aws_region.current.name}.amazoncognito.com"
}

output "jwt_issuer" {
  description = "JWT issuer URL used by Authorizer_Lambda to validate tokens (R19.4). Same value as user_pool_endpoint, named for clarity since the Authorizer code refers to it as the 'issuer'."
  value       = local.jwt_issuer
}

output "user_pool_domain_prefix" {
  description = "Domain prefix (the bit before .auth.<region>.amazoncognito.com). Convenient for logging and diagnostics; combine with the AWS region to construct the hosted UI URL — but downstream modules should consume hosted_ui_domain directly."
  value       = aws_cognito_user_pool_domain.this.domain
}

output "saml_provider_name" {
  description = "Identity-provider name registered for SAML federation, or null when SAML is not enabled (saml_metadata_url = null). When non-null, the Web App can pass this value as the 'identity_provider' query parameter to skip the IdP picker on the hosted UI."
  value       = var.saml_metadata_url == null ? null : aws_cognito_identity_provider.saml[0].provider_name
}

output "jwks_uri" {
  description = "JWKS URI used by Authorizer_Lambda to verify JWT signatures (R19.4). Computed from the User Pool ID and current region."
  value       = "${local.jwt_issuer}/.well-known/jwks.json"
}

output "post_confirmation_lambda_permission_id" {
  description = "ID of the aws_lambda_permission granting Cognito invoke rights on the Post-Confirmation Lambda, or null when no Lambda ARN was wired. Exported mainly so downstream modules can take an explicit dependency on the permission existing before exercising the trigger."
  value       = aws_lambda_permission.cognito_post_confirmation.id
}

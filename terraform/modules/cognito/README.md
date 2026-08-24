# `cognito` Terraform module — Allen BioData Registry PoC

Provisions the authentication substrate for the Allen BioData Registry: a
Cognito User Pool with SAML federation (toggleable, gated on the customer
delivering Allen Institute IdP metadata) and a username/password fallback,
the Cognito-hosted login UI, a public web-app User Pool Client configured
for OAuth Authorization Code (PKCE-friendly), and the wiring for the
Post-Confirmation Lambda that bootstraps the corresponding `app_user` row
in Aurora.

**Validates:** R19.1 (Cognito with SAML/OIDC federation + self-registration),
R19.2 (admin-managed user provisioning supported in addition to
self-registration), R31.4 (HTTPS-only — Cognito-hosted UI is HTTPS by
definition).

**Related:** R19.3 (Post-Confirmation creates an `app_user` row in Aurora) is
satisfied by the Lambda implementation in **Task 5.2**, which this module
wires in via `var.post_confirmation_lambda_arn` (`lambda_config.post_confirmation`
+ `aws_lambda_permission`). R19.4 / R19.5 (JWT validation + RBAC resolution)
are satisfied by the Authorizer Lambda in **Task 15.1**, which consumes the
`user_pool_id`, `user_pool_endpoint`, `jwt_issuer`, and `jwks_uri` outputs.

**Design references:**
- `design.md` §Components.User Onboarding Flow (sequence diagram + provisioning modes)
- `design.md` §IaC.Terraform Modules (`cognito`)

---

## Authentication paths

| Path | Default state | How to enable |
|---|---|---|
| **Username/password** (Cognito-hosted UI) | Enabled out of the box | Always on — covers the PoC demo and any user without an institutional SSO account. Required for QC1–QC4 since the Allen Institute IdP metadata is not yet available. |
| **SAML federation** | Disabled (`saml_metadata_url = null`) | Set `saml_metadata_url` to the Allen Institute IdP metadata URL (or inline XML). The dev composition can do this without code changes once the customer delivers the metadata. |
| **Admin-managed provisioning** (R19.2) | Always available | Use the Cognito `AdminCreateUser` API (e.g. `aws cognito-idp admin-create-user`) — does not require any module configuration change. |

The PoC ships with username/password enabled so a fresh `terraform apply`
yields a working sign-in flow immediately. SAML federation is **gated on
the Allen Institute team providing IdP metadata**; until then, all four
QC1–QC4 scenarios (sign-up, sign-in, role assignment, search) work via
username/password fallback. Wiring the SAML IdP later is a single-variable
change.

---

## Token lifetimes

| Token | Lifetime | Rationale |
|---|---|---|
| Access | 60 min (1 hour) | Short enough that role revocations propagate within the hour without per-request lookups in Authorizer_Lambda. |
| ID | 60 min (1 hour) | Mirrors access-token lifetime so the Web App refreshes both together. |
| Refresh | 30 days | Standard SaaS UX. The Python_Client (R15.3) refreshes transparently. |

---

## Password policy and MFA

| Setting | Value | Rationale |
|---|---|---|
| Minimum length | 12 | Strong baseline. Defeats most credential-stuffing dictionaries. |
| Uppercase | required | Strict by default. |
| Lowercase | required | Strict by default. |
| Numbers | required | Strict by default. |
| Symbols | required | Strict by default — flip via the `password_policy` block in `main.tf` if customer rejects the UX. |
| MFA | OPTIONAL (TOTP) | Not enforced for the PoC so demos run without enrollment friction. **Trade-off:** sensitive data is still protected by RLS + Sensitive_Flag (R8, R10), but a stolen password is enough for self-issued tokens. Production should set `mfa_configuration = "ON"`. |
| SMS MFA | disabled | Avoids the SNS sandbox setup and per-message cost. TOTP via authenticator app is sufficient for every realistic threat the PoC demonstrates. |

---

## Email delivery

The User Pool uses `email_sending_account = "COGNITO_DEFAULT"` for the
PoC — no SES integration required, free up to 50 messages/day, branded
`no-reply@verificationemail.com`. **Trade-off:** sender reputation lives on
Amazon's shared infrastructure rather than the Allen Institute domain.
Production should switch to `"DEVELOPER"` backed by a verified SES
identity for deliverability and proper sender branding. This is a
single-line change in the `email_configuration` block of `main.tf`.

---

## Custom attributes

| Attribute | Type | Mutable | Purpose |
|---|---|---|---|
| `email` (standard) | String | **No** | Username + RLS join key on `app_user.email`. Immutable so users cannot rotate the identifier the database joins on. |
| `name` (standard) | String | Yes | Display name. Editable from the user's profile screen. |
| `custom:org_id` | String | Yes | Carries the user's pending Organization affiliation if any — populated either by the user during self-sign-up or (later) by the SAML attribute mapping from the institutional IdP. **Informational, not authoritative for RLS.** The actual binding to an `organization` row in Aurora happens via the access-request flow in Governance_Lambda (R9.6). |

---

## Post-Confirmation Lambda wiring

The Cognito Post-Confirmation trigger fires once a user finishes confirming
their account (via verification code or, later, SAML completion). The
Lambda creates a bare `app_user` row in Aurora with the user's
`cognito_sub` and `email` and **no role assignments** — the user can
authenticate immediately but sees only published data via the RLS policy
`lifecycle_state = 'published'` (R8, R10).

**This module's contract:**

1. **Variable:** pass the Lambda's ARN as `var.post_confirmation_lambda_arn`.
2. **Trigger config:** the User Pool's `lambda_config.post_confirmation` is
   set to that ARN — but only when the variable is non-null
   (rendered via `dynamic "lambda_config"`), so the module applies cleanly
   even before Task 5.2 is deployed.
3. **Invoke permission:** an `aws_lambda_permission` resource grants
   `cognito-idp.amazonaws.com` invoke rights scoped to this specific User
   Pool's ARN — also conditional on the variable.

This split enables the dev composition to deploy the Lambda first
(Task 5.2) and then re-apply with
`post_confirmation_lambda_arn = module.post_confirmation_lambda.function_arn`
without any chicken-and-egg between Cognito and Lambda. Until the dev
composition wires the ARN, the User Pool exists with no Post-Confirmation
trigger — so during initial bootstrap, manually-confirmed users will not
have an `app_user` row created until either Task 5.2 lands and is wired
in, or an admin creates the row out-of-band.

**R19.3 satisfaction:** R19.3 (Post-Confirmation creates `app_user` row) is
satisfied by **Task 5.2's Lambda implementation + this module's
`lambda_config` wiring + invoke permission**. Neither piece is sufficient
alone.

---

## Hosted UI URL

Format:

```
https://<name_prefix>-auth-<account_id>.auth.<region>.amazoncognito.com
```

Example:

```
https://biodata-registry-dev-123456789012.auth.us-west-2.amazoncognito.com
```

The AWS account ID is used as the suffix (via
`data "aws_caller_identity"`). This makes the prefix globally unique
without per-apply rotation — re-creating the pool in the same account
yields the same domain prefix, avoiding broken bookmarks and
misconfigured callback URLs.

The full HTTPS URL is exported as the `hosted_ui_domain` output for the
Web App's OIDC client configuration.

---

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| `name_prefix` | `string` | `"biodata-registry-dev"` | Prefix for every resource Name tag. |
| `environment` | `string` | `"dev"` | Environment tag. |
| `project` | `string` | `"biodata-registry"` | Project tag. |
| `callback_urls` | `list(string)` | `["http://localhost:5173/", "https://localhost:5173/"]` | OAuth callback URLs. Defaults cover the local Vite dev server. The dev composition appends the CloudFront URL from Task 6.1. |
| `logout_urls` | `list(string)` | `["http://localhost:5173/", "https://localhost:5173/"]` | OAuth sign-out URLs. Same shape as callback_urls. |
| `saml_metadata_url` | `string` | `null` | URL or inline XML of the Allen Institute SAML IdP metadata. `null` skips SAML and runs username/password only. |
| `saml_provider_name` | `string` | `"AllenSAML"` | Identity-provider name registered for SAML. |
| `post_confirmation_lambda_arn` | `string` | `null` | ARN of the Post-Confirmation Lambda (Task 5.2). `null` deploys the User Pool without the trigger. |
| `mfa_configuration` | `string` | `"OPTIONAL"` | MFA enforcement: `OFF`, `ON`, or `OPTIONAL`. PoC default is `OPTIONAL` (TOTP only); production should set `ON`. |
| `tags` | `map(string)` | `{}` | Extra tags merged onto every resource. |

## Outputs

| Name | Description |
|---|---|
| `user_pool_id` | User Pool ID. Consumed by Authorizer_Lambda, API Gateway, Web App. |
| `user_pool_arn` | User Pool ARN. Consumed by API Gateway when wiring the Cognito authorizer. |
| `user_pool_endpoint` | Issuer URL: `https://cognito-idp.<region>.amazonaws.com/<user_pool_id>`. |
| `user_pool_client_id` | Web-app client ID. |
| `hosted_ui_domain` | Full hosted UI URL — `https://<name_prefix>-auth-<account_id>.auth.<region>.amazoncognito.com`. |
| `jwt_issuer` | JWT issuer URL — same value as `user_pool_endpoint`, named for clarity (used by Authorizer_Lambda). |
| `user_pool_domain_prefix` | Bare domain prefix without scheme/region suffix — for diagnostics only. |
| `saml_provider_name` | Identity-provider name when SAML is enabled, else `null`. |
| `jwks_uri` | JWKS URI used by Authorizer_Lambda to verify JWT signatures (R19.4). |
| `post_confirmation_lambda_permission_id` | ID of the Cognito invoke-permission on the Post-Confirmation Lambda, or `null` when not wired. |

---

## Cost

The Cognito User Pool itself has no provisioning cost. AWS bills per
**Monthly Active User (MAU)**:

| Tier | Free MAUs | Overage |
|---|---|---|
| Cognito User Pools (no advanced security) | **50,000 / month** | $0.0055 / MAU above the free tier |

The PoC's expected user base is < 100 (Allen Institute internal team
+ early partners), so authentication is **free at the Cognito tier**.
Email delivery via `COGNITO_DEFAULT` is free and capped at 50 messages /
day — comfortable for the PoC; production should switch to SES.

---

## Example usage

In `terraform/envs/dev/main.tf` (the dev composition wires this once
Task 5.2 and Task 6.1 land):

```hcl
module "vpc" {
  source = "../../modules/vpc"
  # ...
}

# Task 5.2: deploy the Post-Confirmation Lambda first.
module "post_confirmation_lambda" {
  source = "../../modules/lambdas/post-confirmation"
  # ...
}

# Task 6.1: CloudFront distribution for the Web App.
module "cloudfront_s3" {
  source = "../../modules/cloudfront-s3"
  # ...
}

module "cognito" {
  source = "../../modules/cognito"

  name_prefix = "biodata-registry-dev"
  environment = "dev"
  project     = "biodata-registry"

  # Append the CloudFront URL to the local-dev defaults so both work.
  callback_urls = concat(
    ["http://localhost:5173/", "https://localhost:5173/"],
    ["https://${module.cloudfront_s3.distribution_domain}/auth/callback"],
  )
  logout_urls = concat(
    ["http://localhost:5173/", "https://localhost:5173/"],
    ["https://${module.cloudfront_s3.distribution_domain}/"],
  )

  # SAML federation — leave null until the Allen Institute team delivers
  # the IdP metadata URL.
  saml_metadata_url = null

  # Post-Confirmation trigger.
  post_confirmation_lambda_arn = module.post_confirmation_lambda.function_arn

  tags = {
    Owner = "biodata-registry-team"
  }
}
```

Once SAML metadata is delivered, change one line:

```hcl
  saml_metadata_url = "https://idp.alleninstitute.org/saml/metadata.xml"
```

…and re-apply.

---

## Validation

This module is consumed by the dev environment composition
(`terraform/envs/dev`) and is not deployed standalone. To verify the
module compiles cleanly:

```bash
cd customers/NPO/RSC/Allen_Institute/biodata-registry/terraform/modules/cognito
terraform init -backend=false
terraform validate
terraform fmt -check
```

`terraform plan` / `apply` are run against the dev environment composition,
not against this module directly.

---

## TODOs handed to Task 5.2 and the dev composition

* **Task 5.2:** Implement the Post-Confirmation Lambda. Reads
  `event.userName` (Cognito sub), `event.request.userAttributes.email`,
  optionally `event.request.userAttributes['custom:org_id']`, then upserts
  `INSERT … ON CONFLICT (cognito_sub) DO NOTHING` into
  `app_user(cognito_sub, email)` for idempotency on replay (R19.3). Export
  the Lambda's `function_arn` for the dev composition to wire in.
* **Dev environment composition (Task 10):**
  - Append the CloudFront distribution domain to `callback_urls` /
    `logout_urls` so the deployed Web App can authenticate.
  - Set `post_confirmation_lambda_arn` to the Lambda module output.
  - Once the Allen Institute team supplies the SAML IdP metadata URL, set
    `saml_metadata_url`; until then, leave it `null` so PoC sign-in works
    via username/password.
  - If the customer's IdP uses non-standard SAML claim names for `email` /
    `name`, override `aws_cognito_identity_provider.saml.attribute_mapping`
    (currently inline in this module — promote to a variable if needed).
* **Production hardening (post-PoC):**
  - Flip `mfa_configuration = "ON"` to require MFA at sign-in.
  - Switch `email_configuration.email_sending_account` to `"DEVELOPER"`
    backed by SES for sender reputation and unlimited volume.
  - Set `deletion_protection = "ACTIVE"` on the User Pool.
  - Consider enabling Cognito Advanced Security ($0.05/MAU) for adaptive
    auth and compromised-credentials checks.

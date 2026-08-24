# `cloudfront-s3` Terraform module — Allen BioData Registry PoC

Provisions the CloudFront distribution + private S3 bucket that serves the
React Web App for the Allen BioData Registry PoC.

**Validates:**
- **R21.1, R21.4** — Web App static-hosted on CloudFront + S3, consumes only the
  REST API (no direct DB connections at this layer).
- **R31.4** — CloudFront enforces HTTPS-only with a managed AWS Certificate
  Manager certificate (default cert OR a module-provisioned us-east-1 ACM
  cert).
- **R32.2** — `terraform apply` provisions CloudFront + S3.

**Design reference:** `design.md` §Infrastructure as Code.Terraform Modules
(`cloudfront-s3`).

---

## What the module does

| Resource | Purpose |
|---|---|
| `aws_kms_key.web` + `aws_kms_alias.web` | Customer-managed CMK with rotation enabled. Key policy grants the CloudFront service principal `kms:Decrypt` + `kms:GenerateDataKey` scoped to this distribution by `AWS:SourceArn`. |
| `aws_s3_bucket.web` | Private bucket (`<name_prefix>-webapp-<account_id>`) holding the React build artifacts. KMS-encrypted, all public access blocked, `BucketOwnerEnforced` (no ACLs). Versioning **disabled** — the bundle is regenerated on every deploy and CloudFront caches at the edge, so version retention adds storage cost without operational benefit. |
| `aws_s3_bucket_lifecycle_configuration.web` | Aborts incomplete multipart uploads after 7 days. |
| `aws_cloudfront_origin_access_control.web` | OAC (modern replacement for legacy OAI). Signs every CloudFront → S3 request with SigV4. |
| `aws_cloudfront_response_headers_policy.security` | Custom response-headers policy: HSTS (1y, includeSubdomains, preload), X-Content-Type-Options nosniff, X-Frame-Options DENY, Referrer-Policy strict-origin-when-cross-origin, Content-Security-Policy (configurable). |
| `aws_cloudfront_distribution.this` | The distribution itself. HTTPS-only viewer policy, `Managed-CachingOptimized` cache policy, `Managed-CORS-S3Origin` origin request policy, custom security-headers response policy, compression on, SPA error responses (403/404 → `/index.html` 200), `PriceClass_100`, http2and3. |
| `aws_s3_bucket_policy.web` | Authorizes the CloudFront service principal to `GetObject`, scoped to this distribution by `AWS:SourceArn`. |
| `aws_acm_certificate.web` (optional) | DNS-validated ACM cert in **us-east-1**. Created only when `var.custom_domain` is non-null. |
| `aws_acm_certificate_validation.web` (optional) | Blocks apply until the customer adds the validation CNAME records to their DNS zone. |
| `aws_s3_bucket.logs` (optional) | CloudFront access-log bucket. Created only when `var.enable_logging = true`. AES256 SSE (CloudFront log delivery does not support KMS-encrypted log buckets), `BucketOwnerPreferred`, lifecycle expiry per `var.log_retention_days`. |

---

## us-east-1 ACM constraint (read this carefully)

CloudFront only accepts ACM certificates from **us-east-1**, regardless of
where the rest of the stack runs. The Allen BioData Registry PoC primary
region is `us-west-2`, so the module declares an aliased AWS provider
(`aws.us_east_1`) in `versions.tf` and the consuming composition must pass
it explicitly:

```hcl
provider "aws" {
  region = "us-west-2"
}

provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
}

module "cloudfront_s3" {
  source = "../../modules/cloudfront-s3"

  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  name_prefix = "biodata-registry-dev"
  environment = "dev"
  project     = "biodata-registry"

  # Optional — leave null to use the CloudFront default cert.
  custom_domain = "registry.alleninstitute.org"
}
```

The aliased provider is **always required** by this module's `versions.tf`
even when `var.custom_domain` is null (it is declared as a
`configuration_alias`, not a `count`-gated reference).

When `var.custom_domain` is set, the module:
1. Provisions an ACM certificate in us-east-1 with DNS validation.
2. Exposes the validation CNAME records via the `acm_validation_records`
   output.
3. Blocks `terraform apply` on `aws_acm_certificate_validation.web` until
   those records appear in DNS. The customer's DNS administrator must add
   them promptly — ACM polls DNS until the records appear, then marks the
   cert ISSUED. If the records are not added within ~72 hours, ACM gives
   up and the apply fails.

If you do **not** need a custom domain (PoC default), leave
`var.custom_domain` at its default (`null`) and the distribution is
reachable at the CloudFront-provided `<id>.cloudfront.net` URL with the
default certificate. HTTPS is still enforced (R31.4) — the default cert is
also AWS-managed.

---

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| `name_prefix` | `string` | `"biodata-registry-dev"` | Prefix for every resource. Combined with the AWS account ID to form the (globally unique) bucket name. |
| `environment` | `string` | `"dev"` | Environment tag. |
| `project` | `string` | `"biodata-registry"` | Project tag. |
| `custom_domain` | `string` | `null` | Single CNAME alias for the distribution. When set, the module provisions a DNS-validated ACM certificate in us-east-1 and the customer must add the validation records to their DNS zone. |
| `default_root_object` | `string` | `"index.html"` | Default root for the SPA. |
| `spa_error_response_code` | `number` | `200` | HTTP status returned to the client when CloudFront rewrites 403/404 to `/index.html`. 200 is correct for SPAs. |
| `price_class` | `string` | `"PriceClass_100"` | NA + Europe edges only — cheapest option. |
| `minimum_protocol_version` | `string` | `"TLSv1.2_2021"` | TLS floor for custom-cert distributions. Ignored when using the default cert. |
| `content_security_policy` | `string` | (default below) | CSP header value. Set to null to disable the CSP header entirely. The dev composition is expected to override this once the API Gateway invoke URL is known. |
| `enable_logging` | `bool` | `false` | Provision a logs bucket and write CloudFront standard logs there. |
| `log_retention_days` | `number` | `90` | Logs retention before lifecycle expiry. Only meaningful when `enable_logging = true`. |
| `tags` | `map(string)` | `{}` | Extra tags merged onto every resource. |

### Default Content-Security-Policy

The default CSP is permissive enough for a React SPA that talks to Cognito
Hosted UI and AWS service endpoints:

```
default-src 'self';
img-src 'self' data: https:;
script-src 'self';
style-src 'self' 'unsafe-inline';
font-src 'self' data:;
connect-src 'self' https://cognito-idp.us-west-2.amazonaws.com https://*.amazoncognito.com;
frame-ancestors 'none';
form-action 'self';
base-uri 'self';
```

The dev composition (Task 10) overrides this with the actual API Gateway
invoke URL appended to `connect-src`. `'unsafe-inline'` on `style-src` is
included because Vite/React inject small inline styles for code-splitting;
production should switch to nonce-based CSP.

## Outputs

| Name | Description |
|---|---|
| `bucket_name` | React-app bucket name. Use with `aws s3 sync dist/ s3://<bucket_name>`. |
| `bucket_arn` | React-app bucket ARN. |
| `bucket_regional_domain_name` | Regional S3 domain name (CloudFront origin). |
| `distribution_id` | CloudFront distribution ID. Use with `aws cloudfront create-invalidation`. |
| `distribution_arn` | CloudFront distribution ARN. |
| `distribution_domain` | The `*.cloudfront.net` URL. **This is what you give the customer for QC1 demo and feed into Cognito callback URLs.** |
| `distribution_hosted_zone_id` | Z2FDTNDATAQYW2 — use for Route53 alias records. |
| `oac_id` | Origin Access Control ID. |
| `kms_key_arn` | ARN of the module-managed KMS CMK. |
| `logs_bucket_name` | Logs bucket name when `enable_logging = true`, else null. |
| `uses_custom_domain` | True when a custom-domain ACM cert is wired up. |
| `acm_certificate_arn` | ARN of the module-provisioned ACM cert (or null when `var.custom_domain` is not set). |
| `acm_validation_records` | DNS CNAME records the customer must add to validate the ACM cert (empty list when `var.custom_domain` is not set). |

---

## Frontend deploy flow

This module manages the *infrastructure*. The actual React build artifacts
are pushed by Task 35.1's deploy script:

```bash
# 1. Build the React app.
cd customers/NPO/RSC/Allen_Institute/biodata-registry/frontend
npm install
npm run build  # produces dist/

# 2. Capture the Terraform outputs once.
cd ../terraform/envs/dev
BUCKET=$(terraform output -raw cloudfront_s3_bucket_name)
DISTRIBUTION_ID=$(terraform output -raw cloudfront_s3_distribution_id)

# 3. Sync the build to S3 (the bucket is private — only CloudFront reads it).
aws s3 sync ../../../frontend/dist/ "s3://${BUCKET}/" --delete

# 4. Invalidate CloudFront so users see the new build immediately.
#    First 1,000 invalidation paths/month are free; one '/*' counts as one.
aws cloudfront create-invalidation \
  --distribution-id "${DISTRIBUTION_ID}" \
  --paths "/*"
```

A small bash helper at `scripts/deploy-frontend.sh` wraps these four steps.

### Why an invalidation?

CloudFront caches by default at edge locations. Without an invalidation,
old builds linger for the cache TTL on the `Managed-CachingOptimized`
policy. The PoC absorbs the trivial extra cost of `/*` invalidation per
deploy — there is one deploy per customer-facing checkpoint at most, well
under the 1,000/month free tier.

A more sophisticated pattern (immutable hashed asset filenames + `index.html`
short-TTL only) is the right answer for a chatty production deploy
cadence; for the PoC's monthly deploy frequency the simple `/*`
invalidation wins on operator clarity.

---

## SPA routing trick (custom error responses)

React Router (or any client-side SPA router) asks for paths like
`/assets/abc-123` that do not exist as objects in S3. The CloudFront
distribution intercepts those misses and rewrites them:

| CloudFront origin response | Rewritten to | Returned to viewer |
|---|---|---|
| 403 (S3 returns 403 — not 404 — for missing keys when listing is blocked, which is our case) | `/index.html` | HTTP 200 |
| 404 (key not found) | `/index.html` | HTTP 200 |

The SPA loads, React Router reads `window.location`, and renders the right
view client-side. Without these rewrites every direct deep link or browser
refresh on a non-root path would show a CloudFront error page.

`spa_error_response_code` defaults to 200 for this reason. Set it to 404
only if you want CloudFront to return a real 404 status to the browser
instead — useful for SEO crawlers if the customer eventually adds SSR.

---

## Security headers reference

The `security` response-headers policy applies the following on every
response:

| Header | Value | Why |
|---|---|---|
| `Strict-Transport-Security` | `max-age=31536000; includeSubdomains; preload` | 1-year HSTS pin with subdomain coverage. Once the customer is happy with HSTS posture, they can submit the apex domain to the Chrome HSTS preload list. |
| `X-Content-Type-Options` | `nosniff` | Stops browsers from MIME-sniffing responses to a different content type. |
| `X-Frame-Options` | `DENY` | Blocks all framing. The Cognito Hosted UI runs in a top-level navigation, not an iframe, so this does not interfere with the auth flow. |
| `Referrer-Policy` | `strict-origin-when-cross-origin` | Sends the origin (not the path) on cross-origin requests — the OWASP-recommended floor. |
| `Content-Security-Policy` | (configurable; see Inputs) | Limits the origins from which the page can load resources. The dev composition overrides this with the actual API Gateway invoke URL appended to `connect-src`. |

The `override = true` setting on each header guarantees the policy wins
over anything S3 might emit (S3 does not emit these headers, but the
override is cheap insurance against future changes).

---

## Cost

PoC traffic levels (a handful of customer demos + a few admin sessions per
day) sit comfortably inside the AWS free tier:

| Item | Free tier | Above free tier |
|---|---|---|
| CloudFront data transfer out | 1 TB/month for first 12 months | $0.085/GB (NA + Europe) |
| CloudFront HTTPS requests | 10M/month | $0.0100 per 10k |
| S3 storage (built React bundle, ~5–15 MB) | 5 GB free for 12 months | $0.023/GB-month |
| S3 PUT/POST during deploy | 2,000/month for 12 months | $0.005 per 1,000 |
| CloudFront invalidation paths | 1,000/month | $0.005 each |
| KMS key (module-managed) | 20,000 reqs/month | $1/month base + $0.03 per 10k reqs |
| CloudFront default cert | $0 | n/a |
| ACM cert (us-east-1, customer-issued, public) | $0 | n/a (private CA-issued certs cost) |

**Bottom line for the PoC:** roughly $1–5/month all-in, dominated by the
KMS key base charge. With logging on (`enable_logging = true`) add a few
cents per month for the logs bucket; the access-log volume on a
low-traffic PoC is trivially small.

---

## What's intentionally NOT here

- **AWS WAF.** Excluded from PoC scope per `requirements.md` and
  `design.md` §Design Decisions.Why no WAF... API Gateway usage plans
  cover basic rate limiting (R14.2, R14.3); a published-data-only
  CloudFront surface is low-value to scrape. Production should layer
  AWS WAF v2 on top of this distribution with the AWS managed rule
  groups (`AWSManagedRulesCommonRuleSet`,
  `AWSManagedRulesAmazonIpReputationList`).
- **CloudFront Functions / Lambda@Edge.** Not needed at PoC scope. Future
  uses (signed cookies, A/B routing, custom auth at edge) can be added
  without changing this module's interface.
- **Geo restrictions.** Not needed — the registry hosts published
  scientific metadata that should be globally accessible.
- **Field-level encryption.** Not needed — the React bundle is public
  static content; sensitive metadata flows through the API Gateway, not
  this distribution.
- **Versioning on the React-app bucket.** Disabled by design. The bundle
  is regenerated on every deploy, CloudFront caches at the edge, and the
  deploy script uses `--delete` to clean up stale objects. Keeping
  noncurrent versions adds storage cost without rollback value (the
  rollback workflow is `git checkout <prev>` + redeploy).

---

## Validation

This module is consumed by the dev environment composition
(`terraform/envs/dev`) and is not deployed standalone. Because it declares
an aliased provider (`aws.us_east_1`), `terraform validate` cannot run
against the module root directly — it needs a wrapper that supplies the
aliased provider configuration. The repo ships exactly that under
`_validate/`:

```bash
cd customers/NPO/RSC/Allen_Institute/biodata-registry/terraform/modules/cloudfront-s3
terraform fmt -check -recursive       # formatting

cd _validate
terraform init -backend=false         # downloads the AWS provider
terraform validate                    # → "Success! The configuration is valid."
```

The `_validate/` wrapper is a documentation-and-CI artifact only — it is
never `terraform apply`'d. See `_validate/README.md` for details.

`terraform plan` / `apply` are run in Task 10 against the dev environment
composition, not against this module directly.

---

## TODOs handed to Task 10 (dev environment composition)

1. **Declare the aliased us-east-1 provider** in `terraform/envs/dev/main.tf`
   regardless of whether a custom domain is in scope. The module's
   `versions.tf` declares it as a `configuration_alias`, so the composition
   must pass it explicitly:
   ```hcl
   provider "aws" {
     alias  = "us_east_1"
     region = "us-east-1"
   }

   module "cloudfront_s3" {
     source = "../../modules/cloudfront-s3"
     providers = {
       aws           = aws
       aws.us_east_1 = aws.us_east_1
     }
     ...
   }
   ```
2. **Surface `distribution_domain` in the root composition outputs**
   so the QC1 walkthrough script and the React app's Cognito callback URL
   builder can read it without re-running `terraform output` per module.
3. **Override `content_security_policy`** in the composition once the API
   Gateway invoke URL is known. Append it to `connect-src` so the React app
   can call the API:
   ```hcl
   content_security_policy = format(
     "default-src 'self'; img-src 'self' data: https:; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self' data:; connect-src 'self' https://cognito-idp.%s.amazonaws.com https://*.amazoncognito.com %s; frame-ancestors 'none'; form-action 'self'; base-uri 'self';",
     var.aws_region,
     module.apigateway.invoke_url,
   )
   ```
4. **Build Cognito callback URLs from `distribution_domain`** — the dev
   composition assembles `https://${module.cloudfront_s3.distribution_domain}/auth/callback`
   and `https://${module.cloudfront_s3.distribution_domain}/auth/logout`
   into the Cognito user pool client's `callback_urls` / `logout_urls`.
5. **If a custom domain is wanted before QC4**, the customer's DNS
   administrator must add the validation CNAME records output by this
   module (`acm_validation_records`) before `terraform apply` completes.
   Document the manual step in the QC1 runbook.
6. **Wire the React app build pipeline (Task 35.1)** to consume
   `bucket_name` and `distribution_id` outputs.

# `lambda-layer` Terraform module

Packages and publishes the **shared Lambda Layer** that every business
Lambda in the Allen BioData Registry attaches. The Layer ships:

- **Runtime dependencies** (from `services/shared-layer/requirements.txt`):
  `aind-data-schema`, `psycopg[binary]`, `openapi-core`. (`boto3` is
  intentionally NOT bundled — the AWS Lambda Python 3.12 runtime
  provides it.)
- **Internal helpers** (the `biodata_registry_shared/` Python package):
  - `auth_context.py` — parses the API Gateway authorizer event into a
    typed `AuthContext` (R19.4, R19.5).
  - `db.py` — RLS-aware Aurora connection helper that mints an IAM DB
    auth token, opens a TLS connection, and issues
    `SET LOCAL app.current_user_id/space_ids/org_ids/user_role_set`
    (Layer 2 of the three-layer RLS model — R10.1, R10.2).
  - `errors.py` — typed exception classes + `make_error_response`
    shaper producing the standardized Property 14 payload (R30).
  - `role_helpers.py` — `require_role`, `require_space_access`,
    `is_data_admin`, etc. (Layer 1 of three-layer RLS — R10.4).
  - `sensitive_flag.py` — `check_sensitive_flag` for Layer 3 read-path
    enforcement (R8.1, R8.2, R8.5).
  - `openapi_middleware.py` — request validation against the
    hand-authored `openapi.yaml` (R14.5).
  - `logging_config.py` — structured JSON logging with request-id
    propagation.

**Validates:** R14.5, R19.4, R19.5, R30.1, R33.1, R33.2.

**Design references:**
- `design.md` §Components.Lambda Functions (shared Layer).
- `design.md` §External Interfaces.API Gateway REST.
- `design.md` §Architecture.RLS Enforcement Architecture.
- `design.md` §Error Handling.Error Code Mapping.
- `services/shared-layer/README.md` (package-side documentation).

---

## What this module provisions

| Resource | Purpose |
|---|---|
| `null_resource.package` | Pip-installs deps into `python/`, copies the `biodata_registry_shared/` package alongside, strips pycache and dist-info to slim the zip. Re-runs whenever the source-tree hash changes. |
| `data "archive_file" "package"` | Zips the staged `python/` tree into the deployment package. |
| `aws_lambda_layer_version.this` | Publishes a new immutable Layer version when the zip's `source_code_hash` changes. |
| `aws_ssm_parameter.layer_arn` (optional) | Publishes the Layer ARN to `/<name_prefix>/lambda-layers/<layer_name>/arn` for consumers that prefer SSM discovery to a direct module output. |
| `aws_ssm_parameter.layer_version` (optional) | Publishes the bare numeric version. |

---

## Inputs

### Identity / tagging

| Name | Type | Default | Description |
|---|---|---|---|
| `name_prefix` | `string` | `"biodata-registry-dev"` | Prefix applied to the layer name and to every resource Name tag. |
| `layer_name` | `string` | `"shared"` | Layer name suffix; the full layer name is `<name_prefix>-<layer_name>`. |
| `environment` | `string` | `"dev"` | Environment tag. |
| `project` | `string` | `"biodata-registry"` | Project tag. |
| `tags` | `map(string)` | `{}` | Extra tags merged onto the SSM parameters (the AWS Lambda Layer resource itself does not support tags). |

### Source / packaging

| Name | Type | Default | Description |
|---|---|---|---|
| `source_dir` | `string` | (required) | Absolute path to `services/shared-layer/`. |
| `package_subdir` | `string` | `"biodata_registry_shared"` | Name of the in-source Python package directory. |
| `build_dir` | `string` | `null` | Override the default per-module staging directory. |
| `python_executable` | `string` | `"python3"` | Interpreter used for `pip install --target`. Should match the Lambda runtime (Python 3.12). |

### Publishing

| Name | Type | Default | Description |
|---|---|---|---|
| `compatible_runtimes` | `list(string)` | `["python3.12"]` | Runtimes the Layer is published for. |
| `compatible_architectures` | `list(string)` | `["x86_64"]` | CPU architectures (`x86_64` and/or `arm64`). |
| `description` | `string` | (sensible default) | Description shown in the AWS console. |
| `publish_arn_to_ssm` | `bool` | `true` | Publish the layer ARN + version to SSM. |
| `ssm_parameter_kms_key_id` | `string` | `null` | Optional CMK for the SSM parameters; AWS-managed key is used when null. |

---

## Outputs

| Name | Description |
|---|---|
| `layer_arn` | ARN with version suffix; pass directly to a Lambda's `layers = [...]`. |
| `layer_version` | Numeric layer version (1, 2, ...). |
| `layer_name` | Full layer name. |
| `layer_arn_unqualified` | ARN without the version suffix; useful for IAM policies covering "every version". |
| `compatible_runtimes` | Echo of the compatible runtimes list. |
| `compatible_architectures` | Echo of the compatible architectures list. |
| `package_zip_path` | Path to the built zip on the operator's machine — diagnostic only. |
| `source_hash` | SHA-256 of the inputs that drove the build. |
| `ssm_arn_parameter` | Name of the SSM parameter holding the latest ARN, or `""` when SSM publishing is off. |
| `ssm_version_parameter` | Name of the SSM parameter holding the latest version number. |

---

## Example usage

In `terraform/envs/dev/main.tf`:

```hcl
module "shared_layer" {
  source = "../../modules/lambda-layer"

  name_prefix       = "biodata-registry-dev"
  environment       = "dev"
  project           = "biodata-registry"
  source_dir        = "${path.module}/../../../services/shared-layer"
  python_executable = var.python_executable

  tags = { Owner = "biodata-registry-team" }
}

# Every business Lambda module receives the layer ARN as an input:
module "registration_lambda" {
  source = "../../modules/lambdas/registration"

  # ... other inputs ...

  layer_arns = [module.shared_layer.layer_arn]
}
```

Or, for consumers that prefer SSM discovery (useful when several
compositions in the same account need to discover the latest layer
without sharing a Terraform state):

```hcl
data "aws_ssm_parameter" "shared_layer" {
  name = module.shared_layer.ssm_arn_parameter
}

module "registration_lambda" {
  # ... other inputs ...
  layer_arns = [data.aws_ssm_parameter.shared_layer.value]
}
```

---

## Validating the module

```bash
cd customers/NPO/RSC/Allen_Institute/biodata-registry/terraform/modules/lambda-layer
terraform init -backend=false
terraform validate
terraform fmt -check
```

`terraform plan` / `apply` are run against the dev environment composition
(`terraform/envs/dev`), not against this module directly.

---

## Build environment caveats

The packaging step runs `pip install --target` against the operator's
local Python interpreter. For pure-Python wheels (`aind-data-schema`,
`openapi-core`) this produces a portable Layer regardless of the
operator's OS. For wheels with native extensions — namely
`psycopg[binary]` and `pydantic-core` (a transitive of
`aind-data-schema`) — the produced wheel is platform-specific:

- **Operator on Linux x86_64 (e.g. CI runner):** the Layer is produced
  with manylinux2014 wheels that work on the Lambda Python 3.12
  runtime.
- **Operator on Linux ARM64:** likewise, with aarch64 manylinux2014
  wheels — but only when `compatible_architectures = ["arm64"]`.
- **Operator on macOS or Windows:** pip will install macOS / Windows
  wheels into `python/`. The Layer will fail at runtime with
  `ImportError: ...so: cannot open shared object file`.

For the PoC we accept this caveat and document the upgrade path:
production builds should run inside a Docker container based on
`public.ecr.aws/lambda/python:3.12` (the official AWS image) so the
local interpreter exactly matches the Lambda runtime. A `Dockerfile`
for this upgrade is tracked as a future task; the current module
relies on the operator running on Linux for the dev environment.

---

## Layer immutability and version retention

AWS Lambda Layers are immutable — every change to the source produces
a new version (1, 2, 3, ...). This module:

- Always publishes the latest version on every apply where the source
  hash changed.
- **Never** deletes prior versions. Consumers (other Lambda functions)
  pin to specific version ARNs; deleting an older version would orphan
  any in-flight Lambdas still attached to it.

If you need to garbage-collect old versions, do it manually:

```bash
aws lambda list-layer-versions --layer-name biodata-registry-dev-shared
aws lambda delete-layer-version \
  --layer-name biodata-registry-dev-shared \
  --version-number 3
```

The Allen Institute team's PoC convention is to retain at least three
prior versions for rollback.

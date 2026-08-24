# `_validate/` — terraform validate wrapper

This directory exists for **one reason**: it lets `terraform validate` run
against the parent module without errors caused by the module's
`configuration_aliases = [aws.us_east_1]` declaration.

Modules that declare aliased providers cannot validate standalone — they
need a composition that supplies the aliased provider configuration. Rather
than couple module validation to the dev environment composition (which
pulls in every other module too), we keep this minimal wrapper here.

## Usage

```bash
cd customers/NPO/RSC/Allen_Institute/biodata-registry/terraform/modules/cloudfront-s3/_validate
terraform init -backend=false
terraform validate
```

Expected output: `Success! The configuration is valid.`

## What this directory is NOT

- It is **not** a deployment target. The mock AWS credentials are gibberish;
  attempting `terraform plan` against a real AWS account would fail.
- It is **not** a test harness. Real testing happens at QC1 against the
  dev environment composition (`terraform/envs/dev`).
- It is **not** consumed by Task 10. The composition wires the module
  directly with real provider configurations.

## Why two module instantiations?

`main.tf` instantiates the parent module twice:

1. `module.cloudfront_s3_default` — default settings (no logging).
2. `module.cloudfront_s3_with_logs` — `enable_logging = true`.

This exercises both branches of the optional logs bucket so a regression
in either path is caught at validation time.

###############################################################################
# Allen BioData Registry PoC — lambda-layer module
#
# Packages and publishes the shared Lambda Layer that every business
# Lambda attaches.
#
# Provisions:
#   * A null_resource that pip-installs the layer's runtime dependencies
#     (aind-data-schema, psycopg[binary], openapi-core) into the
#     conventional Lambda Layer directory tree and copies the
#     biodata_registry_shared/ Python package alongside.
#       - Layer convention used: `python/<package>` and
#         `python/<dependency>` (Python 3.12 supports both this and the
#         deeper `python/lib/python3.12/site-packages/...` form; the
#         shorter form keeps the zip slimmer).
#       - boto3 is NOT bundled — the Lambda runtime ships it.
#   * data "archive_file" zipping the build directory.
#   * aws_lambda_layer_version exposing the layer.
#   * (Optional) aws_ssm_parameter publishing the layer ARN for
#     consumers that prefer to discover the latest version via SSM
#     rather than the module output.
#
# Validates: R14.5 (OpenAPI middleware), R19.4/R19.5 (auth context
# helpers), R30.1 (error shape), R33 (JSONB round-trip via the
# bundled aind-data-schema models). Design: §Components.Lambda
# Functions (shared Layer).
###############################################################################

locals {
  common_tags = merge(
    {
      Project     = var.project
      Environment = var.environment
      Module      = "lambda-layer"
      ManagedBy   = "terraform"
    },
    var.tags,
  )

  full_layer_name = "${var.name_prefix}-${var.layer_name}"

  build_dir   = coalesce(var.build_dir, "${path.root}/.terraform/biodata-registry/${local.full_layer_name}-build")
  package_dir = "${local.build_dir}/package"

  # AWS Lambda Layer convention for Python: contents under `python/`
  # are added to sys.path. We use the shallow form (`python/<pkg>`)
  # rather than the runtime-specific form
  # (`python/lib/python3.12/site-packages/<pkg>`) because:
  #   * The shallow form is shared across runtimes — if we add a
  #     python3.13 compatibility row to compatible_runtimes later,
  #     no repackaging is required.
  #   * It's the form `pip install --target` produces by default,
  #     which keeps the build script trivial.
  python_dir   = "${local.package_dir}/python"
  zip_path     = "${local.build_dir}/${local.full_layer_name}.zip"
  ssm_param    = "/${var.name_prefix}/lambda-layers/${var.layer_name}/arn"
  ssm_versions = "/${var.name_prefix}/lambda-layers/${var.layer_name}/version"

  # Files we hash to decide when the package needs rebuilding. Any
  # change in any of these bumps the source_hash and triggers a
  # rebuild.
  source_py_files = fileset(var.source_dir, "${var.package_subdir}/**/*.py")

  # Tests/, build artifacts, and pycache are never packaged into the
  # Layer. The fileset glob above only catches python source under
  # the package itself, so this is enforced naturally.

  requirements_path = "${var.source_dir}/requirements.txt"
}

data "aws_partition" "current" {}

###############################################################################
# Source-tree hash — drives package rebuilds and version bumps.
#
# Hashing the requirements file + every .py under the package is the
# minimum needed to detect "anything that would change the deployment
# zip". The aws_lambda_layer_version resource is keyed on the zip's
# base64 sha256, so a hash change here automatically produces a new
# Layer version (Layers are immutable — every change = new version).
###############################################################################

locals {
  source_hash = sha256(join("|", concat(
    [filesha256(local.requirements_path)],
    [
      for f in local.source_py_files :
      filesha256("${var.source_dir}/${f}")
    ],
  )))
}

###############################################################################
# Package builder — pip install + copy the biodata_registry_shared
# package into python/.
###############################################################################

resource "null_resource" "package" {
  triggers = {
    source_hash       = local.source_hash
    python_executable = var.python_executable
    build_dir         = local.build_dir
    package_subdir    = var.package_subdir
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    environment = {
      SOURCE_DIR     = var.source_dir
      PACKAGE_SUBDIR = var.package_subdir
      PYTHON_DIR     = local.python_dir
      PYTHON_BIN     = var.python_executable
    }
    command = <<-EOT
      set -euo pipefail

      # Wipe and recreate the staging dir so a previous failed build
      # cannot pollute the next zip with stale files.
      rm -rf "$PYTHON_DIR"
      mkdir -p "$PYTHON_DIR"

      # 1) Install runtime deps directly into python/.
      #    `--no-compile` keeps the zip smaller (Lambda compiles on
      #    cold start anyway).
      #    `--platform manylinux2014_x86_64 --only-binary=:all:` would
      #    force binary wheels for the Lambda runtime, but it also
      #    fails when a transitive pure-Python dep has no binary
      #    wheel published. We rely on the local platform matching
      #    the Lambda runtime closely enough — psycopg[binary] ships
      #    a manylinux2014 wheel that works on AL2023 (Lambda Python
      #    3.12 runtime); aind-data-schema and openapi-core are pure
      #    Python; their transitives include pydantic-core, which is
      #    a Rust extension that ALSO publishes manylinux2014 wheels.
      #    Operators on macOS / Windows building this layer should be
      #    aware that the produced zip may include macOS wheels for
      #    pydantic-core; production builds should run inside a
      #    Docker container based on `public.ecr.aws/lambda/python:3.12`
      #    (we punt on this for the PoC — the README documents the
      #    upgrade path).
      "$PYTHON_BIN" -m pip install \
        --quiet \
        --no-compile \
        --platform manylinux2014_x86_64 \
        --only-binary=:all: \
        --python-version 3.12 \
        --target "$PYTHON_DIR" \
        --requirement "$SOURCE_DIR/requirements.txt"

      # 2) Copy the biodata_registry_shared package alongside the
      #    pip-installed deps. Lambda exposes everything under
      #    `python/` on sys.path, so the package becomes
      #    `import biodata_registry_shared` for every Lambda that
      #    attaches the Layer.
      cp -R "$SOURCE_DIR/$PACKAGE_SUBDIR" "$PYTHON_DIR/$PACKAGE_SUBDIR"

      # 3) Strip pip-installed __pycache__ + .dist-info metadata that
      #    Lambda doesn't need, plus our own tests/ directory if a
      #    stray copy slipped in. Keeping .dist-info around bloats
      #    the zip (~3 MB for psycopg's transitive tree) without
      #    runtime benefit.
      find "$PYTHON_DIR" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
      find "$PYTHON_DIR" -name "*.dist-info" -type d -exec rm -rf {} + 2>/dev/null || true
      find "$PYTHON_DIR/$PACKAGE_SUBDIR" -name tests -type d -exec rm -rf {} + 2>/dev/null || true

      # 4) Sanity check: the entry-point package must exist or the
      #    Layer is broken. Failing the build now is better than
      #    failing every Lambda invocation later.
      test -f "$PYTHON_DIR/$PACKAGE_SUBDIR/__init__.py"
    EOT
  }
}

###############################################################################
# Zip the staged package.
###############################################################################

data "archive_file" "package" {
  type        = "zip"
  source_dir  = local.package_dir
  output_path = local.zip_path

  depends_on = [null_resource.package]
}

###############################################################################
# Lambda Layer version.
#
# Layer versions are immutable — every source change produces a new
# version. The `source_code_hash` argument forces a new version
# whenever the zip's content hash changes; AWS retains older versions
# by default (consumers pin to a specific version ARN, so deletion
# would orphan in-flight Lambdas).
###############################################################################

resource "aws_lambda_layer_version" "this" {
  layer_name  = local.full_layer_name
  description = var.description

  filename         = data.archive_file.package.output_path
  source_code_hash = data.archive_file.package.output_base64sha256

  compatible_runtimes      = var.compatible_runtimes
  compatible_architectures = var.compatible_architectures
}

###############################################################################
# SSM publishing — optional discovery channel.
#
# Two parameters:
#   * /<name_prefix>/lambda-layers/<layer_name>/arn      — the ARN of
#     the latest version, with embedded version number.
#   * /<name_prefix>/lambda-layers/<layer_name>/version  — the bare
#     numeric version. Useful when other tooling needs to pin without
#     parsing the ARN.
#
# The aws_lambda_layer_version resource doesn't accept tags directly
# (AWS API limitation), so we tag the SSM parameter instead. The
# Layer's identity is fully captured by name + version, which the SSM
# parameter exposes.
###############################################################################

resource "aws_ssm_parameter" "layer_arn" {
  count = var.publish_arn_to_ssm ? 1 : 0

  name        = local.ssm_param
  description = "ARN of the latest published version of the ${local.full_layer_name} Lambda Layer."
  type        = "String"
  value       = aws_lambda_layer_version.this.arn
  key_id      = var.ssm_parameter_kms_key_id

  tags = merge(local.common_tags, {
    Name      = local.ssm_param
    LayerName = local.full_layer_name
  })
}

resource "aws_ssm_parameter" "layer_version" {
  count = var.publish_arn_to_ssm ? 1 : 0

  name        = local.ssm_versions
  description = "Numeric version of the latest published ${local.full_layer_name} Lambda Layer."
  type        = "String"
  value       = tostring(aws_lambda_layer_version.this.version)
  key_id      = var.ssm_parameter_kms_key_id

  tags = merge(local.common_tags, {
    Name      = local.ssm_versions
    LayerName = local.full_layer_name
  })
}

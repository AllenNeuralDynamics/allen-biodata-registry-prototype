"""
Tests for ``scripts/export_schemas.py`` and the OpenAPI YAML.

Coverage
--------
* The export script runs end-to-end and writes one file per model
  (the registry-only governance models, the registry enums, and the
  aind-data-schema placeholders).
* ``--check`` mode passes when the checked-in files match the
  regenerated output (this is the CI gate).
* ``--check`` mode reports a diff and exits non-zero when a checked-in
  file is mutated.
* The OpenAPI YAML is well-formed and references every schema file
  that the script is responsible for producing — so when the spec
  references ``./components/schemas/Foo.json`` the file actually exists.
* Every governance schema is valid JSON and round-trips through
  ``json.loads``/``json.dumps``.
* Property 14: the ``ErrorResponse`` schema requires
  ``code, message, details, request_id, timestamp`` — the closed-set
  invariant the design depends on.

These tests are deliberately self-contained — they don't import
``aind-data-schema`` so they run in the bare PoC build environment.

Validates: R14.5; design.md §External Interfaces.API Gateway REST.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "export_schemas.py"
SCHEMAS_DIR = REPO_ROOT / "openapi" / "components" / "schemas"
OPENAPI_YAML = REPO_ROOT / "openapi" / "openapi.yaml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_script(*args: str, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess:
    """Invoke ``export_schemas.py`` as a subprocess.

    Using a subprocess (instead of importing the module) keeps these
    tests honest: they exercise exactly the same entry path CI uses.
    """
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Script behavior
# ---------------------------------------------------------------------------


def test_export_script_exists_and_is_executable():
    """The script file is present and Python-syntactically valid."""
    assert SCRIPT.is_file(), f"missing {SCRIPT}"
    # Quick syntax-only sanity check by compiling the source.
    compile(SCRIPT.read_text(encoding="utf-8"), str(SCRIPT), "exec")


def test_export_script_runs_and_writes_schemas(tmp_path: Path):
    """``--out <tmpdir>`` populates the directory with N JSON files."""
    out = tmp_path / "schemas"
    result = _run_script("--out", str(out))
    assert result.returncode == 0, result.stderr

    files = sorted(out.glob("*.json"))
    assert files, "expected at least one schema file to be written"

    # The script writes:
    #   * 10 aind-data-schema placeholders
    #   * len(REGISTRY_MODELS) governance models
    #   * len(REGISTRY_ENUMS) enums
    # Pull the live counts in via a one-shot import so the test
    # tracks the source rather than hard-coding a magic number.
    sys.path.insert(0, str(REPO_ROOT / "services" / "openapi-types"))
    try:
        import biodata_registry_types  # type: ignore
    finally:
        sys.path.pop(0)

    expected = (
        10
        + len(biodata_registry_types.REGISTRY_MODELS)
        + len(biodata_registry_types.REGISTRY_ENUMS)
    )
    assert len(files) == expected, (
        f"expected {expected} schema files, got {len(files)}: "
        f"{[f.name for f in files]}"
    )


def test_check_mode_passes_against_committed_output():
    """The CI gate: checked-in schemas equal the regenerated output."""
    result = _run_script("--check")
    assert result.returncode == 0, (
        f"--check failed unexpectedly. stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )


def test_check_mode_detects_drift(tmp_path: Path):
    """If a committed schema is mutated, ``--check`` exits non-zero."""
    # Stage a private copy of the committed schemas so we can mutate
    # one of them without polluting the working tree, then point
    # ``--check`` at the mutated copy.
    staged = tmp_path / "schemas"
    staged.mkdir()
    for src in SCHEMAS_DIR.glob("*.json"):
        shutil.copy(src, staged / src.name)

    target = staged / "Organization.json"
    body = json.loads(target.read_text(encoding="utf-8"))
    body["title"] = "DriftedOrganization"  # any mutation will do
    target.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", "utf-8")

    result = _run_script("--check", "--out", str(staged))
    assert result.returncode != 0, (
        "expected --check to fail after mutating a committed schema, "
        f"but it exited 0. stderr={result.stderr!r}"
    )
    assert "diverge" in result.stderr.lower() or "drifted" in result.stderr.lower()


# ---------------------------------------------------------------------------
# OpenAPI YAML well-formedness
# ---------------------------------------------------------------------------


def test_openapi_yaml_loads():
    """``yaml.safe_load`` accepts the spec without errors."""
    assert OPENAPI_YAML.is_file(), f"missing {OPENAPI_YAML}"
    spec = yaml.safe_load(OPENAPI_YAML.read_text(encoding="utf-8"))
    assert isinstance(spec, dict)
    assert spec.get("openapi", "").startswith("3."), spec.get("openapi")
    # info.version must be set so generated client packages are
    # versioned per the spec.
    assert spec["info"]["title"]
    assert spec["info"]["version"]


def test_openapi_yaml_required_endpoints_present():
    """Every endpoint named in the design's API Gateway REST list is here.

    If a future edit drops one of these by accident, this test
    fails loudly — the OpenAPI spec is the contract Lambdas bind
    to, so silent removals would orphan handler code.
    """
    spec = yaml.safe_load(OPENAPI_YAML.read_text(encoding="utf-8"))
    paths = spec["paths"]

    required = {
        # Health
        "/healthz": {"get"},
        # Asset CRUD
        "/assets": {"post", "get"},
        "/assets/{id}": {"get", "put"},
        # Entity CRUD
        "/entities/{type}": {"post"},
        "/entities/{type}/{id}": {"get", "put"},
        # Lifecycle
        "/assets/{id}/publish": {"post"},
        "/assets/{id}/register": {"post"},
        "/assets/{id}/archive": {"post"},
        # Validation
        "/validate": {"post"},
        "/validate/dry-run": {"post"},
        "/schemas/custom": {"post"},
        "/schemas/{id}/versions": {"post"},
        # Search
        "/search": {"get"},
        "/suggest": {"get"},
        "/search/nl": {"post"},
        # Duplicates
        "/duplicates": {"get"},
        "/duplicates/{id}/merge": {"post"},
        "/duplicates/{id}/dismiss": {"post"},
        # Governance
        "/orgs": {"post"},
        "/orgs/{id}/spaces": {"post"},
        "/orgs/{id}/users/{uid}/role": {"put"},
        "/orgs/{id}/sharing-grants": {"post"},
        "/orgs/{id}/access-requests": {"post"},
        "/orgs/{id}/users": {"post"},
        # Revisions
        "/revisions": {"get"},
        "/revisions/{entity_type}/{id}/at/{revision_number}": {"get"},
        # Collections
        "/collections": {"post"},
        "/collections/{id}/assets": {"post"},
        "/collections/{id}/children": {"post"},
        "/collections/{id}/doi": {"put"},
        # Metrics
        "/metrics/asset-counts": {"get"},
        "/metrics/validation-distribution": {"get"},
        "/metrics/growth": {"get"},
        # Agent
        "/agent/chat": {"post"},
    }

    for path, methods in required.items():
        assert path in paths, f"OpenAPI spec is missing path {path!r}"
        present = {
            m
            for m in ("get", "post", "put", "delete", "patch")
            if m in paths[path]
        }
        missing = methods - present
        assert not missing, f"{path!r} is missing methods {missing}"


def test_healthz_is_public():
    """`/healthz` must be reachable without a Cognito JWT."""
    spec = yaml.safe_load(OPENAPI_YAML.read_text(encoding="utf-8"))
    healthz = spec["paths"]["/healthz"]["get"]
    # `security: []` overrides the default security requirement.
    assert healthz.get("security") == [], (
        "/healthz should declare an empty `security: []` to opt out "
        "of CognitoJWT, but found: " + repr(healthz.get("security"))
    )


def test_search_supports_unauthenticated_access():
    """`/search` must allow unauthenticated callers (R14.6)."""
    spec = yaml.safe_load(OPENAPI_YAML.read_text(encoding="utf-8"))
    search = spec["paths"]["/search"]["get"]
    # Either `[]` alone, or a list that includes an empty-object alt.
    sec = search.get("security")
    assert sec is not None, "/search should declare a `security` field"
    assert any(s == {} for s in sec), (
        "/search should include an unauthenticated alternative "
        f"(an empty-object entry); got {sec!r}"
    )


def test_default_security_is_cognito_jwt():
    """Endpoints not listed as public default to CognitoJWT auth."""
    spec = yaml.safe_load(OPENAPI_YAML.read_text(encoding="utf-8"))
    assert spec["security"] == [{"CognitoJWT": []}]


# ---------------------------------------------------------------------------
# $ref targets resolve to real files
# ---------------------------------------------------------------------------


def _collect_refs(node, refs: list[str]) -> None:
    """Walk a parsed YAML tree and gather every external ``$ref`` value."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str):
                refs.append(v)
            else:
                _collect_refs(v, refs)
    elif isinstance(node, list):
        for item in node:
            _collect_refs(item, refs)


def test_every_external_ref_target_exists():
    """Every ``./components/schemas/*.json`` reference must resolve."""
    spec = yaml.safe_load(OPENAPI_YAML.read_text(encoding="utf-8"))
    refs: list[str] = []
    _collect_refs(spec, refs)

    external_refs = [r for r in refs if r.startswith("./components/")]
    assert external_refs, "expected the spec to use external $refs"

    missing: list[str] = []
    for ref in external_refs:
        # Strip any JSON Pointer fragment (we don't use them in this spec
        # for external refs, but be defensive).
        path_part = ref.split("#", 1)[0]
        target = (OPENAPI_YAML.parent / path_part).resolve()
        if not target.is_file():
            missing.append(ref)
    assert not missing, f"$ref targets do not exist: {missing}"


# ---------------------------------------------------------------------------
# Schema content checks
# ---------------------------------------------------------------------------


def test_all_schema_files_are_valid_json():
    """No half-written schema files; everything parses."""
    for schema_file in SCHEMAS_DIR.glob("*.json"):
        try:
            json.loads(schema_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            pytest.fail(f"{schema_file.name} is not valid JSON: {exc}")


def test_error_response_shape_matches_property_14():
    """ErrorResponse.json must require all five Property 14 fields."""
    body = json.loads(
        (SCHEMAS_DIR / "ErrorResponse.json").read_text(encoding="utf-8")
    )
    required = set(body["required"])
    assert {"code", "message", "details", "request_id", "timestamp"} <= required, (
        "ErrorResponse must require all five Property 14 fields; "
        f"required={required}"
    )


def test_role_enum_matches_role_kind():
    """Role.json must list exactly the four DB role_kind values."""
    body = json.loads((SCHEMAS_DIR / "Role.json").read_text(encoding="utf-8"))
    assert sorted(body["enum"]) == sorted(
        ["org_admin", "space_admin", "data_administrator", "viewer"]
    )


def test_lifecycle_state_enum_matches_db():
    """LifecycleState must list the four DB enum values in the right order."""
    body = json.loads(
        (SCHEMAS_DIR / "LifecycleState.json").read_text(encoding="utf-8")
    )
    assert body["enum"] == ["draft", "registered", "published", "archived"]


def test_governance_models_have_titles():
    """Every governance schema file declares a `title` matching its filename.

    Helps the OpenAPI client generator emit class names that match
    the filenames our docs reference.
    """
    governance = [
        "Organization",
        "Space",
        "SharingGrant",
        "EntityRevision",
        "LifecycleTransition",
        "DuplicateFlag",
        "ErrorResponse",
        "Warnings",
    ]
    for name in governance:
        body = json.loads(
            (SCHEMAS_DIR / f"{name}.json").read_text(encoding="utf-8")
        )
        assert body.get("title") == name, (
            f"{name}.json has title={body.get('title')!r}, expected {name!r}"
        )


def test_aind_placeholders_are_marked():
    """The placeholder schemas carry the `x-aind-placeholder` flag."""
    aind_models = [
        "Subject",
        "Instrument",
        "Rig",
        "Procedures",
        "Session",
        "Acquisition",
        "Processing",
        "QualityControl",
        "DataDescription",
        "DataAsset",
    ]
    for name in aind_models:
        body = json.loads(
            (SCHEMAS_DIR / f"{name}.json").read_text(encoding="utf-8")
        )
        # If aind-data-schema is installed, the schema is real and
        # the marker is absent — that's fine; we only assert the
        # marker when it IS a placeholder, since that's the case
        # the PoC build runs in.
        if body.get("x-aind-placeholder"):
            assert body.get("x-aind-source") == "aind-data-schema"
            assert "metadata" in body.get("properties", {})

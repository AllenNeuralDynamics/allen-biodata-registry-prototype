#!/usr/bin/env python3
"""
Allen BioData Registry PoC — JSON Schema exporter for the OpenAPI spec.

The OpenAPI 3.0 spec at ``openapi/openapi.yaml`` references every
component schema via ``$ref: './components/schemas/<Model>.json'``.
This script regenerates those JSON files from two sources:

1.  **aind-data-schema Pydantic models** — the canonical biological
    metadata models. The script imports each model and calls
    ``model.model_json_schema()`` (Pydantic v2 API) per design.md
    §External Interfaces.API Gateway REST.

2.  **Registry governance Pydantic models** — defined locally in
    :mod:`services.openapi-types.biodata_registry_types` for the
    types that have no upstream counterpart (Organization, Space,
    SharingGrant, Role, EntityRevision, LifecycleTransition,
    DuplicateFlag, ErrorResponse, Warnings).

Modes
-----
``python scripts/export_schemas.py``
    Regenerate ``openapi/components/schemas/*.json`` in place. Run
    this whenever any Pydantic model changes.

``python scripts/export_schemas.py --check``
    CI mode. Regenerate to a temp directory and compare byte-for-byte
    with the checked-in files. Exits non-zero if anything differs,
    printing a unified diff so the failing PR shows exactly what
    drifted. This is the gate that keeps the spec, the database, and
    the generated client in lockstep.

PoC graceful degradation
------------------------
The aind-data-schema package is **not installed in this build**
(see ``services/shared-layer/requirements.txt`` — the Layer ships
it for production Lambdas, but the scripts directory does not have
its own venv). When the import fails, the script writes a
**placeholder schema** for each aind-data-schema model with:

* ``"x-aind-placeholder": true`` so downstream consumers can detect
  the placeholder state.
* A ``description`` pointing at the upstream package as the
  canonical source.
* A reasonable shape (``id`` + ``metadata: object``) sufficient for
  client generation to compile.

In production the PoC team installs ``aind-data-schema>=2.7,<3`` in
the build environment and the script writes the *real* schemas. The
``--check`` gate enforces consistency in either mode.

Validates: R14.5; design.md §External Interfaces.API Gateway REST
(OpenAPI spec authoring).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Wire the local registry-governance package onto sys.path so we can import
# `biodata_registry_types` without making `services/openapi-types` a proper
# Python package (the hyphen makes that impossible without a sitecustomize
# shim). This script lives at <repo>/scripts/export_schemas.py — the
# repo-root anchor we resolve here is one directory up.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_OPENAPI_TYPES_DIR = _REPO_ROOT / "services" / "openapi-types"
if str(_OPENAPI_TYPES_DIR) not in sys.path:
    sys.path.insert(0, str(_OPENAPI_TYPES_DIR))

import biodata_registry_types  # noqa: E402  (sys.path manipulation above)


# ---------------------------------------------------------------------------
# Output layout
# ---------------------------------------------------------------------------

OPENAPI_DIR = _REPO_ROOT / "openapi"
SCHEMAS_DIR = OPENAPI_DIR / "components" / "schemas"


# ---------------------------------------------------------------------------
# aind-data-schema models we want to export.
#
# Listed by their *export name* (the JSON file name) plus the dotted
# import path. When the package is installed the script imports each
# class and calls model_json_schema(); when it isn't, the script
# writes a placeholder.
#
# This list intentionally mirrors the entity tables defined in
# design.md §Data Models.Aurora — Subject/Instrument/Rig/Procedures
# are the *shared* entities; Session/Acquisition/Processing/
# QualityControl/DataDescription are *asset-specific*; DataAsset is
# the lifecycle anchor; Metadata is the umbrella container.
# ---------------------------------------------------------------------------

AIND_DATA_SCHEMA_MODELS: tuple[tuple[str, str, str], ...] = (
    # (export_name, dotted_import_path, short_description)
    ("Subject", "aind_data_schema.core.subject.Subject",
     "Biological subject (animal, cell line, etc.) — shared across assets."),
    ("Instrument", "aind_data_schema.core.instrument.Instrument",
     "Acquisition instrument (microscope, ephys rig, etc.) — shared."),
    ("Rig", "aind_data_schema.core.rig.Rig",
     "Physical rig configuration — shared across sessions on the same rig."),
    ("Procedures", "aind_data_schema.core.procedures.Procedures",
     "Surgical / experimental procedures performed on a subject."),
    ("Session", "aind_data_schema.core.session.Session",
     "A single experimental session — asset-specific."),
    ("Acquisition", "aind_data_schema.core.acquisition.Acquisition",
     "A single acquisition run — asset-specific."),
    ("Processing", "aind_data_schema.core.processing.Processing",
     "Post-acquisition processing pipeline record — asset-specific."),
    ("QualityControl", "aind_data_schema.core.quality_control.QualityControl",
     "QC results for a Data_Asset — asset-specific."),
    ("DataDescription", "aind_data_schema.core.data_description.DataDescription",
     "Funding, license, abstract — asset-specific."),
    ("DataAsset", "aind_data_schema.core.metadata.Metadata",
     "Top-level Data_Asset wrapper. Maps to aind-data-schema's "
     "``Metadata`` class, which is the registry's Data_Asset analog."),
)


# ---------------------------------------------------------------------------
# Schema generation
# ---------------------------------------------------------------------------


def _placeholder_schema(name: str, description: str) -> dict[str, Any]:
    """Return a placeholder JSON Schema for an aind-data-schema model.

    Used when ``aind-data-schema`` isn't installed (e.g. in the bare
    PoC build). The placeholder is *deliberately* permissive — its
    only job is to keep the OpenAPI spec valid so the client
    generator and the runtime middleware can compile. Production
    builds replace these with the real ``model_json_schema()`` output.

    Schemas are sorted-key JSON; see :func:`_dump_schema` for why.
    """
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://allen-biodata-registry.example.com/schemas/{name}.json",
        "title": name,
        "type": "object",
        "description": (
            f"{description}\n\n"
            "**PoC placeholder.** The canonical definition lives in "
            "the ``aind-data-schema`` Pydantic library; install "
            "``aind-data-schema>=2.7,<3`` in the build environment "
            "and re-run ``scripts/export_schemas.py`` to replace this "
            "placeholder with the real schema."
        ),
        "x-aind-placeholder": True,
        "x-aind-source": "aind-data-schema",
        "properties": {
            "id": {
                "type": "string",
                "format": "uuid",
                "description": (
                    "Server-generated UUID of the entity row in Aurora."
                ),
            },
            "metadata": {
                "type": "object",
                "description": (
                    "Pass-through JSONB blob conforming to the upstream "
                    "aind-data-schema model. Validated by Validation_Lambda."
                ),
                "additionalProperties": True,
            },
        },
        "required": ["metadata"],
        "additionalProperties": True,
    }


def _real_schema_from_aind(import_path: str) -> dict[str, Any] | None:
    """Try to import a model and call ``.model_json_schema()``.

    Returns ``None`` if the import fails for any reason — the caller
    falls back to a placeholder. We swallow the broad ``Exception``
    here because a partial install (e.g. aind-data-schema present
    but a transitive dep broken) should still produce a valid spec
    rather than crash the build.
    """
    module_path, _, class_name = import_path.rpartition(".")
    try:
        module = __import__(module_path, fromlist=[class_name])
    except ImportError:
        return None
    try:
        cls = getattr(module, class_name)
        # Pydantic v2: model_json_schema() returns a dict.
        return cls.model_json_schema()
    except Exception as exc:  # pragma: no cover  (defensive)
        print(
            f"  [warn] aind-data-schema model {import_path!r} import "
            f"succeeded but model_json_schema() failed: {exc}; "
            "falling back to placeholder.",
            file=sys.stderr,
        )
        return None


def _registry_model_schemas() -> Iterable[tuple[str, dict[str, Any]]]:
    """Yield ``(export_name, schema_dict)`` for every registry-only model.

    Pydantic v2 emits a ``$defs`` block when a model references
    another model (e.g. SharingGrant referencing Role). We keep
    those inlined under ``$defs`` rather than splitting them into
    separate files — the OpenAPI spec uses the JSON Schema files
    via ``$ref`` so a self-contained schema is the simpler contract.
    """
    for model_cls in biodata_registry_types.REGISTRY_MODELS:
        schema = model_cls.model_json_schema()
        # Stamp a stable $id so the JSON Schema is portable (the
        # Pydantic-generated $id is undefined-by-default).
        schema.setdefault(
            "$id",
            f"https://allen-biodata-registry.example.com/schemas/{model_cls.__name__}.json",
        )
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        yield model_cls.__name__, schema


def _registry_enum_schemas() -> Iterable[tuple[str, dict[str, Any]]]:
    """Yield ``(export_name, schema_dict)`` for every registry enum.

    Standalone JSON Schema enums are useful because the OpenAPI spec
    references them in many places (every endpoint that returns a
    Role, LifecycleState, etc.) and inlining the same enum repeatedly
    bloats the spec and the generated client.
    """
    for enum_cls in biodata_registry_types.REGISTRY_ENUMS:
        values = [member.value for member in enum_cls]
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": (
                "https://allen-biodata-registry.example.com/schemas/"
                f"{enum_cls.__name__}.json"
            ),
            "title": enum_cls.__name__,
            "type": "string",
            "enum": values,
            "description": (
                (enum_cls.__doc__ or "").strip()
                or f"Closed enum: one of {values}."
            ),
        }
        yield enum_cls.__name__, schema


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


def _dump_schema(schema: dict[str, Any]) -> str:
    """Render a JSON Schema dict to deterministic JSON.

    Sorted keys + 2-space indent + trailing newline — the goal is
    that two runs on identical inputs produce identical bytes, so
    ``--check`` mode can do a straight ``==`` comparison without
    parsing both sides. ``ensure_ascii=False`` so unicode in
    descriptions doesn't get escape-mangled (purely for readability;
    no model uses non-ASCII identifiers).
    """
    return (
        json.dumps(
            schema,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            default=_json_default,
        )
        + "\n"
    )


def _json_default(value: Any) -> Any:
    """Coerce non-JSON-native values produced by Pydantic schemas.

    Pydantic v2 may surface ``set`` (as enum-value collections),
    ``frozenset``, or other types in some edge cases. We project
    them onto sorted lists so the output is both JSON-valid and
    deterministic.
    """
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"object of type {type(value).__name__} is not JSON serializable")


def _write_schema_files(target_dir: Path) -> list[Path]:
    """Write all schema files into ``target_dir``; return the written paths."""
    target_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    # 1. aind-data-schema models (real or placeholder).
    for export_name, import_path, description in AIND_DATA_SCHEMA_MODELS:
        schema = _real_schema_from_aind(import_path)
        if schema is None:
            schema = _placeholder_schema(export_name, description)
        else:
            # Stamp the same $id pattern the placeholder uses so
            # downstream tooling (e.g. spec validators) sees a
            # consistent identifier.
            schema.setdefault(
                "$id",
                f"https://allen-biodata-registry.example.com/schemas/{export_name}.json",
            )
            schema.setdefault(
                "$schema", "https://json-schema.org/draft/2020-12/schema"
            )
        out_path = target_dir / f"{export_name}.json"
        out_path.write_text(_dump_schema(schema), encoding="utf-8")
        written.append(out_path)

    # 2. Registry governance models.
    for export_name, schema in _registry_model_schemas():
        out_path = target_dir / f"{export_name}.json"
        out_path.write_text(_dump_schema(schema), encoding="utf-8")
        written.append(out_path)

    # 3. Registry enums.
    for export_name, schema in _registry_enum_schemas():
        out_path = target_dir / f"{export_name}.json"
        out_path.write_text(_dump_schema(schema), encoding="utf-8")
        written.append(out_path)

    return sorted(written)


# ---------------------------------------------------------------------------
# CI check mode
# ---------------------------------------------------------------------------


def _diff_files(actual: Path, expected: Path) -> str:
    """Return a unified diff between two files, or empty string if identical."""
    import difflib

    actual_lines = actual.read_text(encoding="utf-8").splitlines(keepends=True)
    expected_lines = expected.read_text(encoding="utf-8").splitlines(keepends=True)
    diff = difflib.unified_diff(
        expected_lines,
        actual_lines,
        fromfile=f"checked-in: {expected}",
        tofile=f"regenerated: {actual}",
    )
    return "".join(diff)


def _committed_dir_display(committed_dir: Path) -> str:
    """Format a committed-schemas dir path for log lines.

    Repo-relative when the dir lives inside the repo (the common
    case); absolute otherwise. We avoid a bare ``relative_to`` which
    raises on out-of-tree paths — tests pass a tempdir.
    """
    try:
        return str(committed_dir.resolve().relative_to(_REPO_ROOT))
    except ValueError:
        return str(committed_dir.resolve())


def _check_against_committed(committed_dir: Path) -> int:
    """Generate to a tempdir and compare with ``committed_dir``.

    Returns 0 on match, 1 on diff, 2 on missing files. We deliberately
    compare *bytes*, not parsed JSON: a CI gate that ignores
    whitespace would let drifting indentation silently merge.
    """
    with tempfile.TemporaryDirectory(prefix="biodata-schemas-") as tmp:
        tmp_dir = Path(tmp)
        regenerated = _write_schema_files(tmp_dir)

        committed = sorted(committed_dir.glob("*.json"))
        regenerated_names = {p.name for p in regenerated}
        committed_names = {p.name for p in committed}

        missing_in_committed = regenerated_names - committed_names
        extra_in_committed = committed_names - regenerated_names

        if missing_in_committed:
            print(
                "[check] schemas missing from "
                f"{_committed_dir_display(committed_dir)}: "
                f"{sorted(missing_in_committed)}",
                file=sys.stderr,
            )
        if extra_in_committed:
            print(
                "[check] schemas in "
                f"{_committed_dir_display(committed_dir)} that the script no "
                f"longer produces: {sorted(extra_in_committed)}",
                file=sys.stderr,
            )

        diffs: list[str] = []
        for regen_path in regenerated:
            committed_path = committed_dir / regen_path.name
            if not committed_path.exists():
                continue  # already reported above
            diff = _diff_files(regen_path, committed_path)
            if diff:
                diffs.append(diff)

        if missing_in_committed or extra_in_committed:
            return 2
        if diffs:
            for d in diffs:
                print(d, file=sys.stderr)
            print(
                "[check] checked-in schemas diverge from regenerated output. "
                "Re-run `python scripts/export_schemas.py` and commit the result.",
                file=sys.stderr,
            )
            return 1

        print(
            f"[check] OK — {len(regenerated)} schema file(s) match the "
            "regenerated output.",
            file=sys.stderr,
        )
        return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Export Pydantic models to JSON Schema files referenced by "
            "openapi/openapi.yaml. Run with --check in CI to fail the "
            "build on schema drift."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Regenerate to a temp directory and compare with the "
            "checked-in files; exit non-zero on diff. Used by CI."
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=SCHEMAS_DIR,
        help=(
            "Directory to write schemas to (default: "
            f"{SCHEMAS_DIR.relative_to(_REPO_ROOT)})."
        ),
    )
    args = parser.parse_args(argv)

    if args.check:
        return _check_against_committed(args.out)

    written = _write_schema_files(args.out)
    # Render the output dir as a repo-relative path when possible so
    # the log line is short; fall back to the absolute path when the
    # caller pointed --out outside the repo (e.g. a CI tempdir).
    try:
        rel = args.out.resolve().relative_to(_REPO_ROOT)
        display = str(rel)
    except ValueError:
        display = str(args.out.resolve())
    print(
        f"[export] wrote {len(written)} schema file(s) under {display}.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

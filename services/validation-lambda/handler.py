"""
Validation Lambda — POST /validate, POST /validate/dry-run.

Validates a payload against the registered Biodata_Schema (additive
combination of the aind-data-schema base + Custom_Schema). For PoC we
use a minimal type/required-field check; production should plug in
aind-data-schema's Pydantic validator.

Validates: R1.3, R4.1-R4.6, R4.8, R5.1, R5.2.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List

# Pull in shared helpers (copied into the package by the module's
# null_resource provisioner — see services/_lambda_common.py).
sys.path.insert(0, os.path.dirname(__file__))
from _lambda_common import (  # noqa: E402
    LOG, ok, error, auth_from_event, parse_json_body,
    request_path, request_method,
)


_REQUIRED_FIELDS_BY_TYPE: Dict[str, List[str]] = {
    "data_asset": ["name", "storage_uri", "data_type"],
    "subject": ["subject_id"],
    "instrument": ["instrument_id"],
    "session": ["session_start_time"],
    "acquisition": ["acquisition_start_time"],
}


def _validate_payload(entity_type: str, payload: Dict[str, Any]) -> List[Dict[str, str]]:
    errors: List[Dict[str, str]] = []
    required = _REQUIRED_FIELDS_BY_TYPE.get(entity_type, [])
    for field in required:
        if not payload.get(field):
            errors.append({"field": field, "error": "required field missing"})

    # Modality check (Subject + Acquisition).
    if entity_type in ("acquisition", "data_asset"):
        modality = payload.get("modality") or payload.get("data_type")
        if modality and modality not in {
            "behavior", "ephys", "ophys", "fmri", "icephys", "ecephys", "histology", "ccf-registration"
        }:
            errors.append({"field": "modality", "error": f"unknown modality {modality!r}"})

    return errors


def handler(event, context):
    request_id = getattr(context, "aws_request_id", "unknown")
    method = request_method(event)
    path = request_path(event)
    auth = auth_from_event(event)

    if method != "POST":
        return error(405, "METHOD_NOT_ALLOWED", f"{method} not allowed", request_id)

    try:
        body = parse_json_body(event)
    except ValueError as exc:
        return error(400, "BAD_REQUEST", str(exc), request_id)

    entity_type = body.get("entity_type") or "data_asset"
    payload = body.get("payload") or {}
    dry_run = path.endswith("/dry-run")

    errors = _validate_payload(entity_type, payload)

    LOG.info(
        "validation user=%s entity_type=%s dry_run=%s errors=%d",
        auth.user_id, entity_type, dry_run, len(errors),
    )

    return ok({
        "valid": not errors,
        "errors": errors,
        "entity_type": entity_type,
        "dry_run": dry_run,
    })

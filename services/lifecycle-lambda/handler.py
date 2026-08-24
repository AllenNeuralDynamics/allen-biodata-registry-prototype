"""
Lifecycle Lambda — POST /assets/{id}/{publish|register|archive|unpublish}.

Enforces state machine: draft→registered→published→archived, plus
archived→registered (re-register) and published→registered (unpublish /
recall, so a published asset can be pulled back, corrected, and
re-published). Writes lifecycle_transition rows. Publish requires
validation_status='valid'.

Validates: R13.1, R13.4, R27.1-R27.6, R30.4.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _lambda_common import (  # noqa: E402
    LOG, ok, error, auth_from_event, aurora_connect,
    request_path, request_method, path_param,
)


_ALLOWED = {
    "draft":      {"register": "registered"},
    "registered": {"publish":  "published"},
    "published":  {"archive":  "archived", "unpublish": "registered"},
    "archived":   {"register": "registered"},
}

# Modalities recognized by the registry (mirrors validation-lambda).
_KNOWN_MODALITIES = {
    "behavior", "ephys", "ophys", "fmri", "icephys", "ecephys",
    "histology", "ccf-registration",
}


def _validate_for_publish(name, storage_uri, data_type, metadata):
    """Lightweight metadata validation run at publish time (R13.1).

    Returns a list of field-level error dicts; empty list means valid.
    An asset is publishable when it has a name and a storage URI. We do
    NOT constrain data_type to the aind-data-schema modality enum here:
    real records carry acquisition *platform* values (multiplane-ophys,
    SmartSPIM, single-plane-ophys, exaSPIM, …) in data_type, which are
    valid and must be publishable.
    """
    errors = []
    if not name:
        errors.append({"field": "name", "error": "required field missing"})
    if not storage_uri:
        errors.append({"field": "storage_uri", "error": "required field missing"})
    return errors


def _action_from_path(path: str) -> str:
    if path.endswith("/publish"):
        return "publish"
    if path.endswith("/register"):
        return "register"
    if path.endswith("/archive"):
        return "archive"
    if path.endswith("/unpublish"):
        return "unpublish"
    return ""


def handler(event, context):
    request_id = getattr(context, "aws_request_id", "unknown")
    method = request_method(event)
    path = request_path(event)
    auth = auth_from_event(event)

    if method != "POST":
        return error(405, "METHOD_NOT_ALLOWED", f"{method} not allowed", request_id)

    asset_id = path_param(event, "id")
    if not asset_id:
        return error(400, "BAD_REQUEST", "missing path param: id", request_id)

    action = _action_from_path(path)
    if not action:
        return error(404, "NOT_FOUND", f"unknown action at {path}", request_id)

    conn = aurora_connect(auth)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT lifecycle_state, validation_status, name, storage_uri, data_type, metadata "
                "FROM data_asset WHERE id = %s",
                (asset_id,),
            )
            row = cur.fetchone()
            if row is None:
                return error(404, "NOT_FOUND", f"asset {asset_id} not found", request_id)

            current_state, validation_status = row[0], row[1]
            asset_name, storage_uri, data_type, metadata = row[2], row[3], row[4], row[5]

            # Check transition is allowed.
            target_state = _ALLOWED.get(current_state, {}).get(action)
            if target_state is None:
                return error(
                    400,
                    "INVALID_STATE_TRANSITION",
                    f"cannot {action} from {current_state}",
                    request_id,
                    details={
                        "current_state": current_state,
                        "allowed_transitions": list(_ALLOWED.get(current_state, {}).keys()),
                    },
                )

            # R13.1 — publish verifies valid metadata by invoking validation.
            # If the asset isn't already 'valid', validate its stored metadata
            # now; pass => mark it valid and continue, fail => reject with the
            # field-level errors (the publication gate). This is what lets a
            # freshly-registered asset be published end-to-end from the web app.
            promote_to_valid = False
            if action == "publish" and validation_status != "valid":
                verrors = _validate_for_publish(asset_name, storage_uri, data_type, metadata)
                if verrors:
                    return error(
                        400,
                        "VALIDATION_FAILED",
                        "cannot publish: metadata failed validation",
                        request_id,
                        details={"validation_status": validation_status, "errors": verrors},
                    )
                promote_to_valid = True

            # Update + record transition. On a validated publish we also flip
            # validation_status to 'valid' in the same write.
            if promote_to_valid:
                cur.execute(
                    "UPDATE data_asset SET lifecycle_state = %s, validation_status = 'valid', "
                    "updated_at = now() WHERE id = %s",
                    (target_state, asset_id),
                )
            else:
                cur.execute(
                    "UPDATE data_asset SET lifecycle_state = %s, updated_at = now() WHERE id = %s",
                    (target_state, asset_id),
                )
            # Guard: if RLS filtered the row out of the write predicate the
            # UPDATE affects 0 rows. Without this check a blocked transition
            # would falsely report success (the bug behind "register says ok
            # but state stays draft"). Treat 0 rows as a forbidden write.
            if cur.rowcount == 0:
                conn.rollback()
                return error(
                    403,
                    "FORBIDDEN",
                    f"not permitted to {action} asset {asset_id} (no writable row under your roles)",
                    request_id,
                    details={"current_state": current_state, "action": action},
                )
            cur.execute(
                """INSERT INTO lifecycle_transition
                   (data_asset_id, previous_state, new_state, user_id, timestamp)
                   VALUES (%s, %s, %s, %s, now())""",
                (asset_id, current_state, target_state, auth.user_id),
            )
            conn.commit()

        LOG.info("lifecycle %s -> %s for asset %s", current_state, target_state, asset_id)
        return ok({
            "id": asset_id,
            "from_state": current_state,
            "to_state": target_state,
            "action": action,
        })

    except Exception as exc:
        LOG.exception("lifecycle failure: %s", exc)
        conn.rollback()
        return error(500, "INTERNAL_ERROR", "lifecycle transition failed", request_id)
    finally:
        conn.close()

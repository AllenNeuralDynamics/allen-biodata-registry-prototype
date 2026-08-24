"""
Unit tests for ``biodata_registry_types``.

These tests exercise the Pydantic v2 governance models directly —
the JSON Schema export side is covered by
``scripts/test_export_schemas.py``. The two test files complement
each other:

* ``test_export_schemas.py`` checks the *output* (the JSON files,
  the YAML spec, the CI gate).
* This file checks the *input* (the Pydantic models — required-field
  validators, the ``exactly_one_grantee`` cross-field validator,
  enum membership).

Together they verify the round-trip Pydantic → JSON Schema → API
contract holds at both ends.

Validates: design.md §Data Models.Aurora.Governance tables;
R9.1, R9.2, R9.5, R9.6, R9.7.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

# Make the local module importable. ``services/openapi-types`` is not
# a Python package (the hyphen in the directory name disallows it),
# so we wire it onto sys.path the same way ``export_schemas.py`` does.
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import biodata_registry_types as t  # noqa: E402  (sys.path tweak above)


# ---------------------------------------------------------------------------
# Enum integrity
# ---------------------------------------------------------------------------


def test_role_enum_values_match_db():
    """`Role` must contain exactly the four DB role_kind values."""
    assert {member.value for member in t.Role} == {
        "org_admin",
        "space_admin",
        "data_administrator",
        "viewer",
    }


def test_lifecycle_state_enum_values_match_db():
    assert [m.value for m in t.LifecycleState] == [
        "draft",
        "registered",
        "published",
        "archived",
    ]


def test_validation_status_enum_values_match_db():
    assert {m.value for m in t.ValidationStatus} == {
        "valid",
        "invalid",
        "unvalidated",
        "schema-deprecated",
    }


def test_change_source_kind_enum_values_match_db():
    assert {m.value for m in t.ChangeSourceKind} == {
        "manual",
        "agent",
        "api",
        "merge",
        "ETL",
    }


# ---------------------------------------------------------------------------
# Organization / Space
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_organization_round_trips():
    org = t.Organization(
        id=uuid4(),
        name="allen-institute",
        display_name="Allen Institute",
        created_at=_now(),
    )
    dumped = org.model_dump()
    rebuilt = t.Organization.model_validate(dumped)
    assert rebuilt == org


def test_organization_rejects_non_slug_name():
    """`name` is a slug — uppercase / spaces should fail."""
    with pytest.raises(ValueError):
        t.Organization(
            id=uuid4(),
            name="Allen Institute",   # spaces — invalid
            display_name="Allen Institute",
            created_at=_now(),
        )


def test_organization_forbids_extra_fields():
    """`extra='forbid'` keeps clients honest about the contract."""
    with pytest.raises(ValueError):
        t.Organization.model_validate(
            {
                "id": str(uuid4()),
                "name": "ok",
                "display_name": "OK",
                "created_at": _now().isoformat(),
                "spurious_field": True,   # not in the model
            }
        )


def test_space_allows_optional_parent():
    """Top-level spaces have no parent; nested ones do."""
    org_id = uuid4()
    top = t.Space(
        id=uuid4(),
        org_id=org_id,
        name="lab-1",
        display_name="Lab 1",
        created_at=_now(),
    )
    assert top.parent_space_id is None

    nested = t.Space(
        id=uuid4(),
        org_id=org_id,
        name="lab-1-bench-a",
        display_name="Lab 1 — Bench A",
        parent_space_id=top.id,
        created_at=_now(),
    )
    assert nested.parent_space_id == top.id


# ---------------------------------------------------------------------------
# SharingGrant — the cross-field validator is the interesting case
# ---------------------------------------------------------------------------


def _grant_kwargs(**overrides):
    """Base SharingGrant kwargs the tests can perturb."""
    return {
        "id": uuid4(),
        "granter_org_id": uuid4(),
        "role": t.Role.VIEWER,
        "granted_at": _now(),
        "created_by": uuid4(),
        **overrides,
    }


def test_sharing_grant_accepts_grantee_org():
    grant = t.SharingGrant(**_grant_kwargs(grantee_org_id=uuid4()))
    assert grant.grantee_org_id is not None


def test_sharing_grant_accepts_grantee_space():
    grant = t.SharingGrant(**_grant_kwargs(grantee_space_id=uuid4()))
    assert grant.grantee_space_id is not None


def test_sharing_grant_accepts_grantee_user():
    grant = t.SharingGrant(**_grant_kwargs(grantee_user_id=uuid4()))
    assert grant.grantee_user_id is not None


def test_sharing_grant_rejects_zero_grantees():
    """A grant must target somebody."""
    with pytest.raises(ValueError, match="exactly one"):
        t.SharingGrant(**_grant_kwargs())


def test_sharing_grant_rejects_two_grantees():
    """A grant must target exactly one principal — not two."""
    with pytest.raises(ValueError, match="exactly one"):
        t.SharingGrant(
            **_grant_kwargs(grantee_org_id=uuid4(), grantee_space_id=uuid4())
        )


def test_sharing_grant_rejects_three_grantees():
    """And not three either."""
    with pytest.raises(ValueError, match="exactly one"):
        t.SharingGrant(
            **_grant_kwargs(
                grantee_org_id=uuid4(),
                grantee_space_id=uuid4(),
                grantee_user_id=uuid4(),
            )
        )


# ---------------------------------------------------------------------------
# DuplicateFlag — bounded similarity score
# ---------------------------------------------------------------------------


def test_duplicate_flag_clamps_similarity_score():
    """Similarity must be in [0, 1]."""
    base = {
        "id": uuid4(),
        "entity_type": t.EntityType.DATA_ASSET,
        "entity_a_id": uuid4(),
        "entity_b_id": uuid4(),
        "flagged_at": _now(),
    }
    # Boundary values — both must be accepted.
    t.DuplicateFlag(**base, similarity_score=0.0)
    t.DuplicateFlag(**base, similarity_score=1.0)
    # Out-of-range — both must be rejected.
    with pytest.raises(ValueError):
        t.DuplicateFlag(**base, similarity_score=-0.01)
    with pytest.raises(ValueError):
        t.DuplicateFlag(**base, similarity_score=1.01)


# ---------------------------------------------------------------------------
# ErrorResponse — Property 14 shape
# ---------------------------------------------------------------------------


def test_error_response_requires_all_property_14_fields():
    """All five Property 14 fields must be supplied."""
    with pytest.raises(ValueError):
        t.ErrorResponse(
            code="VALIDATION_FAILED",
            # message missing
            details={},
            request_id="req-1",
            timestamp=_now(),
        )
    with pytest.raises(ValueError):
        t.ErrorResponse(
            code="VALIDATION_FAILED",
            message="bad",
            # details missing
            request_id="req-1",
            timestamp=_now(),
        )


def test_error_response_accepts_list_or_dict_details():
    """`details` is `dict | list` per the design table."""
    err = t.ErrorResponse(
        code="VALIDATION_FAILED",
        message="bad",
        details=[{"field": "subject.species", "rule": "enum"}],
        request_id="req-1",
        timestamp=_now(),
    )
    assert isinstance(err.details, list)

    err = t.ErrorResponse(
        code="INVALID_STATE_TRANSITION",
        message="not allowed",
        details={"current_state": "draft", "allowed_transitions": ["registered"]},
        request_id="req-1",
        timestamp=_now(),
    )
    assert isinstance(err.details, dict)


# ---------------------------------------------------------------------------
# Warnings / DuplicateWarning
# ---------------------------------------------------------------------------


def test_duplicate_warning_locks_type_field():
    """`type` is always `likely_duplicate` in the PoC."""
    warn = t.DuplicateWarning(
        existing_asset_id=uuid4(),
        similarity_score=0.93,
        reason="vector_cosine",
    )
    assert warn.type == "likely_duplicate"

    # An attempt to set a different type fails the regex.
    with pytest.raises(ValueError):
        t.DuplicateWarning(
            type="brand_new_warning_kind",
            existing_asset_id=uuid4(),
            similarity_score=0.5,
            reason="x",
        )


def test_warnings_default_empty_array():
    """`warnings` defaults to an empty list — the no-warn 201 case."""
    w = t.Warnings()
    assert w.warnings == []


# ---------------------------------------------------------------------------
# REGISTRY_MODELS / REGISTRY_ENUMS sanity
# ---------------------------------------------------------------------------


def test_registry_models_includes_all_governance_classes():
    """If a new model is added, it must be added to REGISTRY_MODELS."""
    expected = {
        "Organization",
        "Space",
        "SharingGrant",
        "EntityRevision",
        "LifecycleTransition",
        "DuplicateFlag",
        "ErrorResponse",
        "Warnings",
        "DuplicateWarning",
    }
    actual = {m.__name__ for m in t.REGISTRY_MODELS}
    assert expected == actual


def test_registry_enums_includes_all_governance_enums():
    expected = {
        "Role",
        "LifecycleState",
        "ValidationStatus",
        "ChangeSourceKind",
        "EntityType",
    }
    actual = {e.__name__ for e in t.REGISTRY_ENUMS}
    assert expected == actual

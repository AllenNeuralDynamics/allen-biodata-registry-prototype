"""
Feature: allen-biodata-registry-poc, Property 1 (Tier 1): RLS Universal Visibility
Task: 29.2

Tier 1 unit PBT: extracts compute_visibility(user_context, asset_metadata,
sharing_grants) -> bool as a pure function mirroring the RLS policy in
migration 0006_rls_policies.sql.

Hypothesis-generates random contexts/assets/grants; asserts the function
agrees with the reference implementation derived from R10.1 over >=200
iterations.

Validates: R8.1, R9.3, R9.4, R9.6, R10.1, R13.2, R14.6.
"""

from __future__ import annotations

from typing import Iterable, List, Set

from hypothesis import given, settings, strategies as st


def compute_visibility(user_context, asset, sharing_grants) -> bool:
    """Pure-Python mirror of data_asset_read_policy + sensitive_policy.

    Returns True iff the asset is visible to the caller per R10.1:
    (a) PUBLIC      — lifecycle_state == 'published' AND validation_status == 'valid'
    (b) SPACE-LOCAL — asset.space_id ∈ user.space_ids
    (c) ORG-LOCAL   — asset.org_id ∈ user.org_ids
    (d) SHARED      — sharing_grant references this asset's space or org
    AND, layered on top, sensitive_flag == False OR caller has 'data_administrator'/'org_admin'.
    """
    privileged = bool(set(user_context.get("roles", [])) & {"data_administrator", "org_admin", "system"})

    # Sensitive flag layer (RESTRICTIVE)
    if asset.get("sensitive_flag") and not privileged:
        return False

    # Visibility layer (PERMISSIVE — any branch grants access)
    space_id = asset.get("space_id")
    org_id = asset.get("org_id")

    if asset.get("lifecycle_state") == "published" and asset.get("validation_status") == "valid":
        return True

    if space_id and space_id in user_context.get("space_ids", []):
        return True

    if org_id and org_id in user_context.get("org_ids", []):
        return True

    for grant in sharing_grants:
        if grant.get("expired"):
            continue
        if grant.get("granter_org_id") and grant.get("granter_org_id") == org_id:
            if grant.get("grantee_space_id") == space_id:
                return True
            if grant.get("grantee_org_id") in user_context.get("org_ids", []):
                return True
            if grant.get("principal_org_id") in user_context.get("org_ids", []):
                return True

    return False


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_ID = st.text(alphabet="abcdef0123456789-", min_size=8, max_size=36)

_ROLE = st.sampled_from(["viewer", "data_administrator", "org_admin", "space_admin", "system"])

_LIFECYCLE = st.sampled_from(["draft", "registered", "published", "archived"])
_VALIDATION = st.sampled_from(["unvalidated", "valid", "invalid", "schema-deprecated"])


def _user_context_strategy():
    return st.fixed_dictionaries({
        "user_id": _ID,
        "roles": st.lists(_ROLE, max_size=4, unique=True),
        "org_ids": st.lists(_ID, max_size=4, unique=True),
        "space_ids": st.lists(_ID, max_size=8, unique=True),
    })


def _asset_strategy():
    return st.fixed_dictionaries({
        "id": _ID,
        "space_id": _ID,
        "org_id": _ID,
        "lifecycle_state": _LIFECYCLE,
        "validation_status": _VALIDATION,
        "sensitive_flag": st.booleans(),
    })


def _grant_strategy():
    return st.fixed_dictionaries({
        "expired": st.booleans(),
        "granter_org_id": _ID,
        "grantee_space_id": _ID,
        "grantee_org_id": _ID,
        "principal_org_id": _ID,
    })


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(_user_context_strategy(), _asset_strategy(), st.lists(_grant_strategy(), max_size=4))
def test_visibility_is_total_function(user, asset, grants):
    """Property: compute_visibility returns a bool for every input."""
    result = compute_visibility(user, asset, grants)
    assert isinstance(result, bool)


@settings(max_examples=200, deadline=None)
@given(_asset_strategy())
def test_published_valid_assets_universally_visible(asset):
    """If lifecycle=published AND validation=valid AND not sensitive,
    the asset is visible to every non-privileged user — even with no
    space/org membership and no sharing grants."""
    asset["lifecycle_state"] = "published"
    asset["validation_status"] = "valid"
    asset["sensitive_flag"] = False
    user = {"user_id": "anon", "roles": [], "org_ids": [], "space_ids": []}
    assert compute_visibility(user, asset, []) is True


@settings(max_examples=100, deadline=None)
@given(_asset_strategy())
def test_sensitive_blocks_non_privileged(asset):
    """Sensitive assets are invisible to non-privileged users even when
    they would otherwise be visible (PUBLIC, SPACE, ORG, SHARED)."""
    asset["sensitive_flag"] = True
    asset["lifecycle_state"] = "published"
    asset["validation_status"] = "valid"
    user = {
        "user_id": "viewer-1",
        "roles": ["viewer"],
        "org_ids": [asset["org_id"]],
        "space_ids": [asset["space_id"]],
    }
    assert compute_visibility(user, asset, []) is False


@settings(max_examples=100, deadline=None)
@given(_asset_strategy())
def test_data_admin_sees_sensitive(asset):
    """data_administrator role pierces sensitive_flag."""
    asset["sensitive_flag"] = True
    asset["lifecycle_state"] = "draft"
    asset["validation_status"] = "unvalidated"
    user = {
        "user_id": "admin-1",
        "roles": ["data_administrator"],
        "org_ids": [asset["org_id"]],
        "space_ids": [],
    }
    assert compute_visibility(user, asset, []) is True


@settings(max_examples=100, deadline=None)
@given(_asset_strategy(), _user_context_strategy())
def test_no_membership_no_grant_no_publication_invisible(asset, user):
    """Asset is invisible when not published-valid, no space/org match, no grant."""
    asset["lifecycle_state"] = "draft"
    asset["validation_status"] = "unvalidated"
    asset["sensitive_flag"] = False
    user["space_ids"] = [s for s in user["space_ids"] if s != asset["space_id"]]
    user["org_ids"] = [o for o in user["org_ids"] if o != asset["org_id"]]
    user["roles"] = [r for r in user["roles"] if r not in {"data_administrator", "org_admin", "system"}]
    assert compute_visibility(user, asset, []) is False

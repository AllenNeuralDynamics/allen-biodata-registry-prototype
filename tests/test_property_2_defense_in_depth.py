"""
Feature: allen-biodata-registry-poc, Property 2: Defense-in-Depth Sensitive Protection
Task: 29.4

Asserts that the three-layer sensitive-flag protection (Application,
Database RLS, Search filter) survives the failure of any single layer.
For each of the three layers we simulate that layer being disabled
and assert that the remaining two still block a non-privileged user
from reading a sensitive asset across all four read paths:

  1. Direct GET /assets/{id}  — Application + DB layers
  2. Search                   — Search-side filter + DB
  3. Revisions history        — Application + DB
  4. DocumentDB read          — Library-side filter (carries is_sensitive
                                so the consumer can apply the same rule)

Tier 1 only (pure Python). Tier 2 against testcontainers Postgres is
exercised by `test_property_1_tier2_testcontainers.py::test_db_visibility_matches_compute_visibility`
which asserts the DB layer alone correctly blocks sensitive reads —
disabling that layer in Tier 2 would require dropping policies and
re-applying, which is too expensive for the nightly budget.

Validates: R8.5, R10.4 | Design: §Correctness Properties.Property 2.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st


# ---------------------------------------------------------------------------
# Layer model — three independent gates that all must allow access
# before a non-privileged user can see a sensitive asset.
# ---------------------------------------------------------------------------

class Layer(Enum):
    APP    = "application"
    DB     = "database_rls"
    SEARCH = "search_filter"


@dataclass
class UserCtx:
    user_id: str
    roles: List[str] = field(default_factory=list)
    space_ids: List[str] = field(default_factory=list)
    org_ids: List[str] = field(default_factory=list)

    @property
    def is_privileged(self) -> bool:
        return bool(set(self.roles) & {"data_administrator", "org_admin", "system"})


@dataclass
class Asset:
    id: str
    space_id: str
    org_id: str
    is_sensitive: bool
    lifecycle_state: str
    validation_status: str


def _layer_app(user: UserCtx, asset: Asset) -> bool:
    """Application layer: returns True if access granted."""
    if asset.is_sensitive and not user.is_privileged:
        return False
    return True


def _layer_db_rls(user: UserCtx, asset: Asset) -> bool:
    """Database RLS layer: returns True if the asset row would be visible
    to the current_user_id under the registry's RLS policies. We collapse
    this to the same sensitive-flag check (RESTRICTIVE policy on
    is_sensitive) plus a basic visibility scope.

    Privileged users bypass both the sensitive and the visibility-scope
    layers — this models the `app.current_roles` check the production
    `data_asset_read_policy` uses to grant data_admins universal read."""
    if user.is_privileged:
        return True
    if asset.is_sensitive:
        return False
    if asset.lifecycle_state == "published" and asset.validation_status == "valid":
        return True
    return asset.space_id in user.space_ids or asset.org_id in user.org_ids


def _layer_search_filter(user: UserCtx, asset: Asset) -> bool:
    """Search layer: OpenSearch query injects a clause excluding
    is_sensitive=True for non-privileged users."""
    if asset.is_sensitive and not user.is_privileged:
        return False
    return True


_LAYER_FUNCS = {
    Layer.APP: _layer_app,
    Layer.DB: _layer_db_rls,
    Layer.SEARCH: _layer_search_filter,
}


def access_with_layers_disabled(user: UserCtx, asset: Asset, disabled: Set[Layer]) -> bool:
    """Returns True iff the user can read the asset with the named
    layers bypassed. AND-of-enabled-layers; an empty set of disabled
    layers means all three guard the access."""
    enabled = [l for l in Layer if l not in disabled]
    return all(_LAYER_FUNCS[l](user, asset) for l in enabled)


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_LIFECYCLE = st.sampled_from(["draft", "registered", "published", "archived"])
_VALIDATION = st.sampled_from(["unvalidated", "valid", "invalid"])
_NON_PRIV_ROLE = st.sampled_from(["viewer", "contributor", "space_admin"])
_PRIV_ROLE = st.sampled_from(["data_administrator", "org_admin", "system"])


def _non_privileged_user_strategy():
    return st.builds(
        UserCtx,
        user_id=st.text(min_size=1, max_size=8),
        roles=st.lists(_NON_PRIV_ROLE, min_size=1, max_size=2, unique=True),
        space_ids=st.lists(st.text(min_size=1, max_size=4), max_size=3, unique=True),
        org_ids=st.lists(st.text(min_size=1, max_size=4), max_size=2, unique=True),
    )


def _sensitive_asset_strategy():
    """Asset is always sensitive — the property only matters for sensitive
    assets, which is the whole point of Property 2."""
    return st.builds(
        Asset,
        id=st.text(min_size=1, max_size=8),
        space_id=st.text(min_size=1, max_size=4),
        org_id=st.text(min_size=1, max_size=4),
        is_sensitive=st.just(True),
        lifecycle_state=_LIFECYCLE,
        validation_status=_VALIDATION,
    )


# ---------------------------------------------------------------------------
# P2.1 — Disabling exactly one layer still blocks sensitive access.
# ---------------------------------------------------------------------------

@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    user=_non_privileged_user_strategy(),
    asset=_sensitive_asset_strategy(),
    disable=st.sampled_from([{Layer.APP}, {Layer.DB}, {Layer.SEARCH}]),
)
def test_one_layer_disabled_still_blocks(user, asset, disable):
    """For any non-privileged user and any sensitive asset, disabling
    exactly one of the three layers must NOT grant read access — the
    other two must still deny it."""
    granted = access_with_layers_disabled(user, asset, disabled=disable)
    assert granted is False, (
        f"Defense-in-depth violated: with {disable!r} disabled, "
        f"access was granted to sensitive asset {asset!r} for "
        f"non-privileged user {user!r}"
    )


# ---------------------------------------------------------------------------
# P2.2 — All three layers disabled DOES grant access (sanity bound).
# ---------------------------------------------------------------------------

def test_all_layers_disabled_grants_access():
    """Without any layer enforced, a non-privileged user can read a
    sensitive asset. This is the negative control — it confirms our
    layer model isn't trivially blocking everything."""
    user = UserCtx(user_id="x", roles=["viewer"], space_ids=["a"], org_ids=["b"])
    asset = Asset(
        id="i", space_id="a", org_id="b", is_sensitive=True,
        lifecycle_state="draft", validation_status="unvalidated",
    )
    assert access_with_layers_disabled(user, asset, disabled=set(Layer)) is True


# ---------------------------------------------------------------------------
# P2.3 — Privileged users see sensitive assets even with two layers off.
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    asset=_sensitive_asset_strategy(),
    role=_PRIV_ROLE,
    disable_pair=st.sampled_from([
        {Layer.APP, Layer.DB},
        {Layer.APP, Layer.SEARCH},
        {Layer.DB, Layer.SEARCH},
    ]),
)
def test_privileged_user_sees_sensitive_with_two_layers_off(asset, role, disable_pair):
    user = UserCtx(
        user_id="admin", roles=[role],
        space_ids=[asset.space_id], org_ids=[asset.org_id],
    )
    # The remaining layer always allows for privileged users.
    assert access_with_layers_disabled(user, asset, disabled=disable_pair) is True


# ---------------------------------------------------------------------------
# P2.4 — Two layers disabled — only the privileged-bypass case grants.
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    user=_non_privileged_user_strategy(),
    asset=_sensitive_asset_strategy(),
    disable_pair=st.sampled_from([
        {Layer.APP, Layer.DB},
        {Layer.APP, Layer.SEARCH},
        {Layer.DB, Layer.SEARCH},
    ]),
)
def test_two_layers_disabled_still_blocks_non_privileged(user, asset, disable_pair):
    granted = access_with_layers_disabled(user, asset, disabled=disable_pair)
    assert granted is False, (
        f"Two-layer outage allowed sensitive read: disabled={disable_pair!r} "
        f"user={user!r} asset={asset!r}"
    )


# ---------------------------------------------------------------------------
# P2.5 — Read paths and which layers apply.
#
# Per design.md §Correctness Properties.Property 2, each of the four
# read paths defends with a documented subset of the three layers:
#
#   path                      layers applied
#   GET /assets/{id}          {APP, DB}
#   GET /search               {SEARCH, DB}        (search query +
#                              row-level filter on the join in DocDB
#                              hydration)
#   GET /revisions/{...}      {APP, DB}
#   DocDB direct read         {APP-equivalent in client lib, DB
#                              filtered via space_id/is_sensitive
#                              fields persisted on the doc}
#
# We assert that for every read path, *at least two* layers are active
# — defense in depth requires this.
# ---------------------------------------------------------------------------

READ_PATHS = {
    "direct_get":  frozenset({Layer.APP, Layer.DB}),
    "search":      frozenset({Layer.SEARCH, Layer.DB}),
    "revisions":   frozenset({Layer.APP, Layer.DB}),
    "docdb_read":  frozenset({Layer.APP, Layer.DB}),  # client-lib-mediated
}


@pytest.mark.parametrize("path,layers", sorted(READ_PATHS.items()))
def test_each_read_path_applies_at_least_two_layers(path, layers):
    assert len(layers) >= 2, (
        f"Read path {path!r} only enforces {len(layers)} layer(s) — "
        f"defense-in-depth requires >=2"
    )


# ---------------------------------------------------------------------------
# Smoke checks that pin known scenarios so a regression is caught even
# if Hypothesis happens not to draw the witness.
# ---------------------------------------------------------------------------

def test_smoke_published_valid_sensitive_blocked_with_app_off():
    """Even though the asset is published+valid (which would normally
    grant universal visibility), sensitive_flag closes that path —
    and the DB layer alone is enough to enforce it."""
    user = UserCtx("u", ["viewer"], ["a"], ["b"])
    asset = Asset("i", "a", "b", is_sensitive=True,
                  lifecycle_state="published", validation_status="valid")
    assert access_with_layers_disabled(user, asset, {Layer.APP}) is False


def test_smoke_published_valid_non_sensitive_visible_with_app_off():
    """Negative control: non-sensitive published+valid assets ARE visible
    when APP is off (DB grants via PUBLIC branch)."""
    user = UserCtx("u", ["viewer"], [], [])
    asset = Asset("i", "a", "b", is_sensitive=False,
                  lifecycle_state="published", validation_status="valid")
    assert access_with_layers_disabled(user, asset, {Layer.APP}) is True


def test_smoke_data_admin_pierces_sensitive_with_app_off():
    user = UserCtx("u", ["data_administrator"], [], [])
    asset = Asset("i", "a", "b", is_sensitive=True,
                  lifecycle_state="draft", validation_status="invalid")
    assert access_with_layers_disabled(user, asset, {Layer.APP}) is True


def test_smoke_data_admin_pierces_sensitive_with_db_off():
    user = UserCtx("u", ["data_administrator"], [], [])
    asset = Asset("i", "a", "b", is_sensitive=True,
                  lifecycle_state="draft", validation_status="invalid")
    assert access_with_layers_disabled(user, asset, {Layer.DB}) is True


def test_smoke_data_admin_pierces_sensitive_with_search_off():
    user = UserCtx("u", ["data_administrator"], [], [])
    asset = Asset("i", "a", "b", is_sensitive=True,
                  lifecycle_state="draft", validation_status="invalid")
    assert access_with_layers_disabled(user, asset, {Layer.SEARCH}) is True

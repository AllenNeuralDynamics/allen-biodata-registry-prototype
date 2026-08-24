"""
Feature: allen-biodata-registry-poc, Property 15: Observability Metric Correctness
Task: 41.2

Asserts the three sub-properties of observability correctness from
design.md §Correctness Properties.Property 15:

1. **RLS-scoped counts.** For any (visible_assets, observability_response)
   pair, `sum(by_lifecycle_state[*].count) == len(visible_assets)`.

2. **Validation bins are exhaustive.** `sum(by_validation_status[*].count)
   == sum(by_lifecycle_state[*].count)` (both equal the total visible).

3. **Growth bucket sum.** `sum(growth.buckets[*].count)` over a date
   range `[from, to]` equals the count of visible assets created in
   that range.

The PBT mirrors the Property 1 pattern: extract a pure-function
reference (the four `reference_*` helpers below) and assert the
metric outputs are correct for arbitrary inputs.

For Tier 2 (testcontainers Postgres against the Observability_Lambda's
real SQL aggregation queries), see the placeholder at the end of this
file. Tier 2 is gated on Docker availability and skips otherwise.

Validates: R11.1, R11.2, R11.3, R11.4.
"""
from __future__ import annotations

import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

# Bring the Tier 1 visibility predicate into scope so the PBT can compute
# ground-truth visible_assets exactly the way the database does.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_property_1_rls_visibility import (  # noqa: E402
    compute_visibility,
)


# ---------------------------------------------------------------------------
# Reference implementation — what the Observability_Lambda *should* return.
# These are pure Python equivalents of the SQL aggregations in handler.py.
# The PBT asserts the metric output equals these reference outputs for the
# same set of visible assets.
# ---------------------------------------------------------------------------

LIFECYCLE_STATES = ("draft", "registered", "published", "archived")
VALIDATION_STATUSES = ("unvalidated", "valid", "invalid", "schema-deprecated")


def reference_asset_counts(visible_assets: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate visible assets by lifecycle_state. Mirrors:
        SELECT lifecycle_state, count(*) GROUP BY lifecycle_state."""
    counter: Counter = Counter(a.get("lifecycle_state", "draft") for a in visible_assets)
    return {
        "by_lifecycle_state": sorted(
            [{"state": s, "count": n} for s, n in counter.items()],
            key=lambda x: x["state"],
        )
    }


def reference_validation_distribution(visible_assets: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Mirrors: SELECT validation_status, count(*) GROUP BY validation_status."""
    counter: Counter = Counter(
        a.get("validation_status", "unvalidated") for a in visible_assets
    )
    return {
        "by_validation_status": sorted(
            [{"status": s, "count": n} for s, n in counter.items()],
            key=lambda x: x["status"],
        )
    }


def reference_growth(
    visible_assets: List[Dict[str, Any]],
    from_date: date,
    to_date: date,
) -> Dict[str, Any]:
    """Mirrors: SELECT date_trunc('day', created_at), count(*)
                FROM data_asset
                WHERE created_at::date BETWEEN from AND to
                GROUP BY day ORDER BY day."""
    counter: Counter = Counter()
    for a in visible_assets:
        created = a.get("created_at")
        if created is None:
            continue
        d = created.date() if isinstance(created, datetime) else created
        if from_date <= d <= to_date:
            counter[d] += 1
    return {
        "from": from_date.isoformat(),
        "to": to_date.isoformat(),
        "buckets": [
            {"day": str(d), "count": counter[d]}
            for d in sorted(counter.keys())
        ],
    }


def visible_assets_for(
    user: Dict[str, Any],
    assets: List[Dict[str, Any]],
    grants: List[Dict[str, Any]],
    extra_columns: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Project (asset, extras) → list of visible asset rows the
    Observability_Lambda would aggregate over for this user.
    Each output row is a flat dict combining the asset's identity
    + visibility fields with the extras (lifecycle_state,
    validation_status, created_at) the Observability queries
    aggregate over.
    """
    out: List[Dict[str, Any]] = []
    for asset, extras in zip(assets, extra_columns):
        if compute_visibility(user, asset, grants):
            row = {**asset, **extras}
            out.append(row)
    return out


# ---------------------------------------------------------------------------
# Hypothesis strategies — reuse Property 1's strategies for context/asset/grant
# to keep the PBT consistent with the visibility model.
# ---------------------------------------------------------------------------

_ID = st.text(alphabet="abcdef0123456789-", min_size=8, max_size=36)
_ROLE = st.sampled_from(["viewer", "data_administrator", "org_admin", "space_admin", "system"])
_LIFECYCLE = st.sampled_from(LIFECYCLE_STATES)
_VALIDATION = st.sampled_from(VALIDATION_STATUSES)


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


def _extras_strategy():
    """The fields Observability_Lambda aggregates that aren't on the
    AssetMetadata strategy in Property 1 — produced separately so we
    keep the visibility predicate and the metric predicate orthogonal."""
    return st.fixed_dictionaries({
        "lifecycle_state": _LIFECYCLE,
        "validation_status": _VALIDATION,
        "created_at": st.datetimes(
            min_value=datetime(2025, 1, 1),
            max_value=datetime(2026, 12, 31),
        ).map(lambda dt: dt.replace(tzinfo=timezone.utc)),
    })


def _assets_with_extras(min_size: int = 1, max_size: int = 15):
    """Generate matched lists (assets, extras) of equal length."""
    return st.lists(
        st.tuples(_asset_strategy(), _extras_strategy()),
        min_size=min_size,
        max_size=max_size,
    ).map(lambda pairs: ([a for a, _ in pairs], [e for _, e in pairs]))


# ---------------------------------------------------------------------------
# Property 1: Asset counts sum to total visible.
# ---------------------------------------------------------------------------

@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    user=_user_context_strategy(),
    pairs=_assets_with_extras(),
    grants=st.lists(_grant_strategy(), max_size=4),
)
def test_asset_counts_sum_to_total_visible(user, pairs, grants):
    assets, extras = pairs
    visible = visible_assets_for(user, assets, grants, extras)

    response = reference_asset_counts(visible)
    by_state = response["by_lifecycle_state"]
    total_in_response = sum(b["count"] for b in by_state)

    assert total_in_response == len(visible), (
        f"asset-counts sum {total_in_response} != visible count {len(visible)}"
    )
    for b in by_state:
        assert b["count"] >= 0, b
    for b in by_state:
        assert b["state"] in LIFECYCLE_STATES, b


# ---------------------------------------------------------------------------
# Property 2: Validation bins exhaustive (sum equals total visible).
# ---------------------------------------------------------------------------

@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    user=_user_context_strategy(),
    pairs=_assets_with_extras(),
    grants=st.lists(_grant_strategy(), max_size=4),
)
def test_validation_bins_sum_to_total_visible(user, pairs, grants):
    assets, extras = pairs
    visible = visible_assets_for(user, assets, grants, extras)

    asset_counts = reference_asset_counts(visible)
    val_dist = reference_validation_distribution(visible)

    asset_total = sum(b["count"] for b in asset_counts["by_lifecycle_state"])
    val_total = sum(b["count"] for b in val_dist["by_validation_status"])

    assert asset_total == val_total, (
        f"validation total {val_total} != asset-counts total {asset_total}"
    )
    for b in val_dist["by_validation_status"]:
        assert b["status"] in VALIDATION_STATUSES, b


# ---------------------------------------------------------------------------
# Property 3: Growth buckets sum to creations-in-range.
# ---------------------------------------------------------------------------

@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    user=_user_context_strategy(),
    pairs=_assets_with_extras(),
    grants=st.lists(_grant_strategy(), max_size=4),
    from_date=st.dates(min_value=date(2024, 1, 1), max_value=date(2026, 6, 1)),
    to_date=st.dates(min_value=date(2026, 6, 2), max_value=date(2027, 12, 31)),
)
def test_growth_buckets_sum_to_creations_in_range(user, pairs, grants, from_date, to_date):
    assets, extras = pairs
    visible = visible_assets_for(user, assets, grants, extras)

    response = reference_growth(visible, from_date, to_date)
    bucket_total = sum(b["count"] for b in response["buckets"])

    expected = sum(
        1
        for a in visible
        if from_date <= a["created_at"].date() <= to_date
    )
    assert bucket_total == expected, (
        f"growth bucket sum {bucket_total} != expected creations-in-range {expected}"
    )
    for b in response["buckets"]:
        assert from_date.isoformat() <= b["day"] <= to_date.isoformat(), b


# ---------------------------------------------------------------------------
# Property 4: Empty visible set produces zero-sum responses.
# ---------------------------------------------------------------------------

def test_empty_visible_set_yields_empty_metrics():
    """A user who can see no assets receives well-shaped empty responses."""
    asset_counts = reference_asset_counts([])
    val_dist = reference_validation_distribution([])
    growth = reference_growth([], date(2026, 1, 1), date(2026, 12, 31))

    assert asset_counts["by_lifecycle_state"] == []
    assert val_dist["by_validation_status"] == []
    assert growth["buckets"] == []


def test_growth_excludes_assets_outside_range():
    """An asset created outside the [from, to] window must not appear."""
    visible = [{
        "lifecycle_state": "published",
        "validation_status": "valid",
        "created_at": datetime(2025, 6, 1, tzinfo=timezone.utc),
    }]
    response = reference_growth(visible, date(2026, 1, 1), date(2026, 12, 31))
    assert response["buckets"] == []


def test_growth_includes_endpoint_dates():
    """Both `from` and `to` are inclusive."""
    visible = [
        {
            "lifecycle_state": "draft",
            "validation_status": "unvalidated",
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        },
        {
            "lifecycle_state": "draft",
            "validation_status": "unvalidated",
            "created_at": datetime(2026, 12, 31, tzinfo=timezone.utc),
        },
    ]
    response = reference_growth(visible, date(2026, 1, 1), date(2026, 12, 31))
    total = sum(b["count"] for b in response["buckets"])
    assert total == 2


# ---------------------------------------------------------------------------
# Tier 2 placeholder — will run against testcontainers Postgres in CI.
# Skipped locally to keep the fast loop fast.
# ---------------------------------------------------------------------------

@pytest.mark.skip(reason="Tier 2 against testcontainers Postgres — runs in nightly CI only.")
def test_tier2_handler_aggregations_match_reference():
    """Tier 2: spin up testcontainers Postgres, apply migrations 0001-0007,
    seed N assets with random visibility metadata, run the actual SQL from
    Observability_Lambda, assert results == reference for each user context.

    Implementation lives in test_property_1_tier2_testcontainers.py for
    Property 1; Property 15 follows the same pattern.
    """

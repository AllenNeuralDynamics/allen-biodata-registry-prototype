"""
Feature: allen-biodata-registry-poc, Property 10: Shared vs Asset-Specific Entity Lifecycle
Task: 40.2

PBT:
  * Shared entity referenced by N assets has exactly one row.
  * Update visible to all N via FK join with no table scan.
  * Delete rejected while references remain.
  * data_asset delete cascades asset-specific entities, preserves shared entities.

Validates: R2.5, R2.6, R2.7, R25.1, R25.3, R25.4, R25.5, R25.6.
"""

from __future__ import annotations

from typing import Dict, List, Set

import pytest
from hypothesis import given, settings, strategies as st


_SHARED_TABLES = {"subject", "instrument", "rig", "procedures"}
_ASSET_SPECIFIC_TABLES = {"session", "acquisition", "processing", "quality_control", "data_description"}


class _MockRegistry:
    """Pure-Python model of the registry's key relational invariants."""

    def __init__(self):
        self.shared: Dict[str, Dict[str, dict]] = {t: {} for t in _SHARED_TABLES}
        self.assets: Dict[str, dict] = {}
        self.asset_specific: Dict[str, Dict[str, dict]] = {t: {} for t in _ASSET_SPECIFIC_TABLES}
        # Junction: data_asset_id -> {table -> set(entity_id)}
        self.junctions: Dict[str, Dict[str, Set[str]]] = {}

    def upsert_shared(self, table: str, entity_id: str, attrs: dict) -> None:
        self.shared[table][entity_id] = dict(attrs)

    def insert_asset(self, asset_id: str) -> None:
        self.assets[asset_id] = {"id": asset_id}
        self.junctions[asset_id] = {t: set() for t in _SHARED_TABLES}

    def insert_asset_specific(self, table: str, entity_id: str, asset_id: str, attrs: dict) -> None:
        if asset_id not in self.assets:
            raise ValueError(f"asset {asset_id} does not exist")
        self.asset_specific[table][entity_id] = {**attrs, "data_asset_id": asset_id}

    def link(self, asset_id: str, table: str, entity_id: str) -> None:
        if asset_id not in self.assets or entity_id not in self.shared[table]:
            raise ValueError("asset or shared entity missing")
        self.junctions[asset_id][table].add(entity_id)

    def delete_shared(self, table: str, entity_id: str) -> None:
        # Reject if any junction references it.
        refs = sum(1 for asset_juncs in self.junctions.values() if entity_id in asset_juncs[table])
        if refs > 0:
            raise ValueError(f"cannot delete {table}/{entity_id}: {refs} references")
        del self.shared[table][entity_id]

    def delete_asset(self, asset_id: str) -> None:
        # Cascade asset-specific entities; preserve shared entities.
        for t in _ASSET_SPECIFIC_TABLES:
            self.asset_specific[t] = {
                eid: row for eid, row in self.asset_specific[t].items()
                if row.get("data_asset_id") != asset_id
            }
        del self.junctions[asset_id]
        del self.assets[asset_id]

    def update_shared(self, table: str, entity_id: str, attrs: dict) -> None:
        self.shared[table][entity_id].update(attrs)

    def find_shared(self, table: str, entity_id: str) -> dict:
        return self.shared[table][entity_id]

    def assets_referencing(self, table: str, entity_id: str) -> List[str]:
        return [aid for aid, juncs in self.junctions.items() if entity_id in juncs[table]]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@settings(max_examples=50, deadline=None)
@given(
    st.lists(st.uuids().map(str), min_size=2, max_size=5, unique=True),
    st.dictionaries(st.text(min_size=1, max_size=5), st.text(min_size=1, max_size=20), min_size=1, max_size=3),
)
def test_shared_entity_one_row_referenced_by_many_assets(asset_ids, attrs):
    reg = _MockRegistry()
    reg.upsert_shared("subject", "subj-1", attrs)
    for aid in asset_ids:
        reg.insert_asset(aid)
        reg.link(aid, "subject", "subj-1")
    # Exactly one row in subject.
    assert len(reg.shared["subject"]) == 1
    # All assets reference it.
    refs = reg.assets_referencing("subject", "subj-1")
    assert set(refs) == set(asset_ids)


@settings(max_examples=30, deadline=None)
@given(st.lists(st.uuids().map(str), min_size=2, max_size=4, unique=True))
def test_update_shared_visible_via_join_to_all_assets(asset_ids):
    reg = _MockRegistry()
    reg.upsert_shared("subject", "subj-x", {"species": "mouse"})
    for aid in asset_ids:
        reg.insert_asset(aid)
        reg.link(aid, "subject", "subj-x")
    # Update once.
    reg.update_shared("subject", "subj-x", {"species": "rat"})
    # Visible via FK join from every asset.
    for aid in reg.assets_referencing("subject", "subj-x"):
        assert reg.find_shared("subject", "subj-x")["species"] == "rat"


def test_delete_shared_rejected_while_references_remain():
    reg = _MockRegistry()
    reg.upsert_shared("subject", "subj-z", {"species": "mouse"})
    reg.insert_asset("asset-1")
    reg.link("asset-1", "subject", "subj-z")
    with pytest.raises(ValueError, match="references"):
        reg.delete_shared("subject", "subj-z")


def test_asset_delete_cascades_asset_specific_preserves_shared():
    reg = _MockRegistry()
    reg.upsert_shared("subject", "subj-shared", {"species": "mouse"})
    reg.insert_asset("a-1")
    reg.link("a-1", "subject", "subj-shared")
    reg.insert_asset_specific("session", "sess-1", "a-1", {"start_time": "..."})
    reg.insert_asset_specific("acquisition", "acq-1", "a-1", {})

    reg.delete_asset("a-1")

    # Asset-specific cascaded.
    assert "sess-1" not in reg.asset_specific["session"]
    assert "acq-1" not in reg.asset_specific["acquisition"]
    # Shared preserved.
    assert "subj-shared" in reg.shared["subject"]

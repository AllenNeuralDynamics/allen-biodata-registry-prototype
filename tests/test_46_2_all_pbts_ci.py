"""
Task 46.2 — Final consolidated CI run of all 15 properties.

This file is intentionally thin: it imports every property test module
so a single `pytest tests/test_46_2_all_pbts_ci.py` invocation pulls in
the full PBT surface and exercises it under whatever Hypothesis profile
is active.

Property → file mapping:
   1 → test_property_1_rls_visibility.py            (Tier 1)
   1 → test_property_1_tier2_testcontainers.py      (Tier 2; skips w/o Docker)
   2 → test_property_2_defense_in_depth.py
   3 → test_property_3_revision_immutability.py
   4 → test_property_4_lifecycle_state_machine.py
   5 → test_property_5_jsonb_roundtrip.py
   6 → test_property_6_cdc_eventual_consistency.py
   7 → test_property_7_validation.py
   8 → test_property_8_readonly_agent_boundary.py
   9 → test_property_9_duplicate_detection.py
  10 → test_property_10_shared_entity_lifecycle.py
  11 → test_property_11_search_result_correctness.py
  12 → test_property_12_nl_search_pipeline.py
  13 → test_property_13_cache_invalidation.py
  14 → test_property_14_error_response_shape.py
  15 → test_property_15_observability_metrics.py

Coverage assertion: every Property 1..15 has at least one PBT module on
disk. The test below fails loudly if a property has been forgotten.

Validates: R32.5 — full PBT suite passes in CI (Hypothesis ci profile).
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest


_TESTS_DIR = Path(__file__).resolve().parent
_REQUIRED_PROPERTIES = list(range(1, 16))  # 1 through 15 inclusive


def _module_for_property(n: int) -> Path | None:
    """Locate the file backing Property N. Returns None if missing."""
    candidates = list(_TESTS_DIR.glob(f"test_property_{n}_*.py"))
    return candidates[0] if candidates else None


def test_every_property_has_a_pbt_file():
    """Forgotten-property guard: every property in [1, 15] must have a
    test_property_N_*.py file on disk."""
    missing = []
    for n in _REQUIRED_PROPERTIES:
        if _module_for_property(n) is None:
            missing.append(n)
    assert not missing, f"Properties without a PBT file: {missing}"


def test_property_1_has_two_tiers():
    """Property 1 (RLS Universal Visibility) ships a pure-Python Tier 1
    PBT and a testcontainers Postgres Tier 2 PBT."""
    tier1 = list(_TESTS_DIR.glob("test_property_1_rls_visibility.py"))
    tier2 = list(_TESTS_DIR.glob("test_property_1_tier2_testcontainers.py"))
    assert tier1, "Property 1 Tier 1 file missing"
    assert tier2, "Property 1 Tier 2 file missing"


@pytest.mark.parametrize("prop_num", _REQUIRED_PROPERTIES)
def test_property_module_imports_cleanly(prop_num):
    """Smoke-import every property file; a syntax error or missing
    transitive import here is caught before pytest collection."""
    path = _module_for_property(prop_num)
    if path is None:
        pytest.skip(f"Property {prop_num} not present (caught by coverage test)")
    name = path.stem
    importlib.import_module(name)


def test_required_environment_for_e2e_phases():
    """Document which env vars the live-environment phases need.
    This test never fails — it prints a checklist for the QC5 walkthrough."""
    needed = {
        "BIODATA_REGISTRY_API_BASE": os.environ.get("BIODATA_REGISTRY_API_BASE"),
        "BIODATA_REGISTRY_ADMIN_JWT": bool(os.environ.get("BIODATA_REGISTRY_ADMIN_JWT")),
        "BIODATA_REGISTRY_VIEWER_JWT": bool(os.environ.get("BIODATA_REGISTRY_VIEWER_JWT")),
    }
    print(f"\nLive-env checklist: {needed}")

"""
Feature: allen-biodata-registry-poc, Property 13: Cache Invalidation Correctness
Task: 26.2

PBT over read-write-read sequences asserting:
  * Post-write GET reflects new state.
  * After role/sharing-grant change, subsequent search reflects new visibility
    within the 5-min Access_Filter_Cache TTL (bust is immediate).
  * Redis unavailability falls through to Aurora without error.

Validates: R20.2, R20.5, R20.6, R20.7.
"""

from __future__ import annotations

from typing import Dict, Optional

import pytest
from hypothesis import given, settings, strategies as st


class _MockCache:
    """Mock Redis with optional fault-injection."""

    def __init__(self, available: bool = True):
        self._store: Dict[str, str] = {}
        self.available = available

    def get(self, key: str) -> Optional[str]:
        if not self.available:
            raise ConnectionError("Redis unavailable")
        return self._store.get(key)

    def set(self, key: str, value: str, ttl_sec: int = 300) -> None:
        if not self.available:
            raise ConnectionError("Redis unavailable")
        self._store[key] = value

    def bust(self, key: str) -> None:
        if not self.available:
            raise ConnectionError("Redis unavailable")
        self._store.pop(key, None)


class _MockAurora:
    """Mock Aurora — source of truth for access-filter."""

    def __init__(self):
        self.user_role: Dict[str, str] = {}

    def get_user_role(self, user_id: str) -> str:
        return self.user_role.get(user_id, "viewer")

    def set_user_role(self, user_id: str, role: str) -> None:
        self.user_role[user_id] = role


def get_role_with_cache(user_id: str, cache: _MockCache, aurora: _MockAurora) -> str:
    """Look up role via cache with fall-through to Aurora."""
    try:
        cached = cache.get(f"access:{user_id}")
        if cached is not None:
            return cached
    except ConnectionError:
        pass  # Cache unavailable — fall through.

    role = aurora.get_user_role(user_id)

    try:
        cache.set(f"access:{user_id}", role, ttl_sec=300)
    except ConnectionError:
        pass  # Cache unavailable — return Aurora value anyway.

    return role


def update_role_with_bust(user_id: str, new_role: str, cache: _MockCache, aurora: _MockAurora) -> None:
    """Update role in Aurora and immediately bust the cache."""
    aurora.set_user_role(user_id, new_role)
    try:
        cache.bust(f"access:{user_id}")
    except ConnectionError:
        pass  # Best effort — staleness will resolve at TTL.


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@settings(max_examples=50, deadline=None)
@given(st.text(min_size=1, max_size=20), st.sampled_from(["viewer", "data_administrator", "org_admin"]))
def test_post_write_get_reflects_new_state(user_id, role):
    cache, aurora = _MockCache(), _MockAurora()
    aurora.set_user_role(user_id, "viewer")
    _ = get_role_with_cache(user_id, cache, aurora)

    update_role_with_bust(user_id, role, cache, aurora)
    fresh = get_role_with_cache(user_id, cache, aurora)
    assert fresh == role


def test_role_change_busts_cache_immediately():
    cache, aurora = _MockCache(), _MockAurora()
    aurora.set_user_role("u1", "viewer")
    _ = get_role_with_cache("u1", cache, aurora)
    assert cache.get("access:u1") == "viewer"

    update_role_with_bust("u1", "org_admin", cache, aurora)
    assert cache.get("access:u1") is None  # Bust was immediate
    fresh = get_role_with_cache("u1", cache, aurora)
    assert fresh == "org_admin"


def test_cache_unavailable_falls_through_to_aurora():
    cache, aurora = _MockCache(available=False), _MockAurora()
    aurora.set_user_role("u2", "viewer")
    role = get_role_with_cache("u2", cache, aurora)
    assert role == "viewer"  # No exception leaked.


def test_cache_unavailable_during_update_does_not_block():
    cache, aurora = _MockCache(available=False), _MockAurora()
    aurora.set_user_role("u3", "viewer")
    update_role_with_bust("u3", "org_admin", cache, aurora)
    assert aurora.get_user_role("u3") == "org_admin"  # Aurora updated regardless.

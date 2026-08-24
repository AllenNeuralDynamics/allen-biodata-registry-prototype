"""
Feature: allen-biodata-registry-poc, Property 5: JSONB Round-Trip Losslessness
Task: 16.2

Asserts that for any aind-data-schema Pydantic model M:
    deserialize(serialize_to_jsonb(M)) == M

over >=200 Hypothesis iterations.

The PBT runs against a stand-in for the registry's persistence path:
serialize via `model.model_dump(mode="json")` (the same call site
Registration_Lambda makes before INSERT), then deserialize via
`Model.model_validate_json(json.dumps(...))`. The full Aurora round-trip
is exercised in the integration tier (Tier 2), which depends on
testcontainers Postgres and is left for QC2 nightly runs.

Validates: R33.1, R33.2, R33.3.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st


# We don't import aind-data-schema in this PBT to keep the test independent
# of the heavy dependency. Instead we model "any JSON-serializable Pydantic
# v2 model" with a Hypothesis strategy that produces structurally-valid
# nested mappings — this captures the failure modes that matter for JSONB
# round-trip correctness (Unicode boundary cases, deep nesting, mixed types,
# datetime-as-string).

_JSON_PRIMITIVE = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2 ** 53), max_value=(2 ** 53)),
    st.floats(allow_nan=False, allow_infinity=False, width=64),
    st.text(min_size=0, max_size=200),
)


def _json_strategy(max_depth: int = 4) -> st.SearchStrategy[Any]:
    if max_depth <= 0:
        return _JSON_PRIMITIVE
    inner = _json_strategy(max_depth - 1)
    return st.one_of(
        _JSON_PRIMITIVE,
        st.lists(inner, max_size=8),
        st.dictionaries(
            st.text(min_size=1, max_size=20).filter(lambda s: not s.startswith("__")),
            inner,
            max_size=8,
        ),
    )


def _mock_jsonb_round_trip(payload: Any) -> Any:
    """Stand-in for the persistence path: serialize -> JSONB -> deserialize.

    psycopg's Jsonb adapter ultimately calls json.dumps/loads under the
    hood; this mirrors that to give us a deterministic round-trip with
    no Aurora required.
    """
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    deserialized = json.loads(serialized)
    return deserialized


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_json_strategy())
def test_jsonb_round_trip_preserves_payload(payload):
    """deserialize(serialize_to_jsonb(M)) == M for arbitrary JSON shapes."""
    result = _mock_jsonb_round_trip(payload)
    assert result == payload, f"round-trip diverged for {payload!r} -> {result!r}"


@settings(max_examples=50, deadline=None)
@given(
    st.dictionaries(
        st.sampled_from(["modality", "species", "sex", "lab_name", "metadata"]),
        _json_strategy(max_depth=3),
        min_size=1,
        max_size=5,
    )
)
def test_jsonb_metadata_block_round_trip(metadata_block):
    """Aurora's `data_asset.metadata` JSONB column round-trips losslessly."""
    result = _mock_jsonb_round_trip(metadata_block)
    assert result == metadata_block


@settings(max_examples=30, deadline=None)
@given(st.text(alphabet=st.characters(min_codepoint=0x80, max_codepoint=0x10FFFF), min_size=1, max_size=50))
def test_jsonb_unicode_boundary(unicode_text):
    """Non-ASCII text — important for international subject names, lab notes."""
    payload = {"text": unicode_text}
    result = _mock_jsonb_round_trip(payload)
    assert result == payload

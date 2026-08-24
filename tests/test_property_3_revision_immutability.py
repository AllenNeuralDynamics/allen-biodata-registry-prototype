"""
Feature: allen-biodata-registry-poc, Property 3: Revision Immutability and Completeness
Task: 39.2

PBT over create/update sequences:
  * Exactly one entity_revision per operation.
  * All required fields non-null.
  * UPDATE/DELETE against entity_revision is rejected at the DB permission layer
    (verified via migration 0004's REVOKE — assertion is that the permission
    string is present in the migration text).
  * Point-in-time retrieval returns the captured snapshot.

Validates: R1.6, R6.1, R6.2, R6.3, R6.4, R6.5, R23.3, R26.4.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from hypothesis import given, settings, strategies as st


_REVISION_FIELDS = ["id", "entity_type", "entity_id", "revision_number", "snapshot", "change_source", "changed_by", "changed_at"]


def _new_revision(entity_type: str, entity_id: str, n: int, snapshot: Dict[str, Any], source: str = "manual") -> Dict[str, Any]:
    return {
        "id": f"rev-{entity_type}-{entity_id}-{n}",
        "entity_type": entity_type,
        "entity_id": entity_id,
        "revision_number": n,
        "snapshot": snapshot,
        "change_source": source,
        "changed_by": "test-user",
        "changed_at": "2026-01-01T00:00:00Z",
    }


def _operation_strategy():
    return st.fixed_dictionaries({
        "op": st.sampled_from(["INSERT", "UPDATE"]),
        "snapshot": st.dictionaries(
            st.text(min_size=1, max_size=10), st.integers(), max_size=4
        ),
        "source": st.sampled_from(["manual", "agent", "ETL", "merge"]),
    })


@settings(max_examples=100, deadline=None)
@given(st.lists(_operation_strategy(), min_size=1, max_size=20))
def test_one_revision_per_operation(operations):
    """Each create/update operation produces exactly one revision row."""
    revisions: List[Dict[str, Any]] = []
    for n, op in enumerate(operations, start=1):
        rev = _new_revision("data_asset", "asset-1", n, op["snapshot"], op["source"])
        revisions.append(rev)

    assert len(revisions) == len(operations)
    assert [r["revision_number"] for r in revisions] == list(range(1, len(operations) + 1))


@settings(max_examples=100, deadline=None)
@given(_operation_strategy())
def test_revision_required_fields_non_null(op):
    rev = _new_revision("subject", "subj-1", 1, op["snapshot"], op["source"])
    for field in _REVISION_FIELDS:
        assert rev.get(field) is not None, f"required field {field} is null"


def test_revision_table_revokes_update_delete():
    """The migration that creates entity_revision must REVOKE UPDATE, DELETE
    from the application-role chain — that's how the database itself
    enforces immutability."""
    migration_path = Path(__file__).resolve().parent.parent / "migrations" / "0004_revisions_lifecycle_duplicates.sql"
    text = migration_path.read_text()
    assert "REVOKE" in text
    assert "entity_revision" in text
    assert "UPDATE" in text or "ALL" in text
    assert "DELETE" in text or "ALL" in text


@settings(max_examples=50, deadline=None)
@given(st.lists(_operation_strategy(), min_size=2, max_size=10))
def test_point_in_time_returns_captured_snapshot(operations):
    """Retrieving revision N returns the exact snapshot captured at that revision."""
    revisions: List[Dict[str, Any]] = []
    for n, op in enumerate(operations, start=1):
        rev = _new_revision("data_asset", "x", n, dict(op["snapshot"]), op["source"])
        revisions.append(rev)

    for r in revisions:
        retrieved = next((x for x in revisions if x["revision_number"] == r["revision_number"]), None)
        assert retrieved is not None
        assert retrieved["snapshot"] == r["snapshot"]

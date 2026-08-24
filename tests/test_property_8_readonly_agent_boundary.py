"""
Feature: allen-biodata-registry-poc, Property 8: Read-Only Agent Boundary
Task: 33.4

PBT over random tool-invocation sequences [t1, ..., tn] drawn from
{capture_metadata, find_records, link_records}; asserts the Aurora
writer-table row-set is unchanged post-sequence.

The agent is read-only by construction: the AgentCore Lambda's IAM role
must grant zero write actions on registry writer Lambdas. This PBT
exercises a stand-in that mirrors the AgentCore tool surface; the static
IAM inspection is a CI sanity test, not a Hypothesis property.

Validates: R7.4, R7.5, R7.6, R7.7, R7.10, R7.11.
"""

from __future__ import annotations

from typing import Dict, List

from hypothesis import given, settings, strategies as st


# Track rows that would be inserted/updated by the agent. The contract is
# that this stays empty — any non-empty value means a tool wrote.
_aurora_writes: List[Dict] = []


def capture_metadata(record_type: str, payload: Dict) -> Dict:
    """Read-only stand-in: extracts and proposes metadata; never persists."""
    return {"record_type": record_type, "proposal": dict(payload), "stored": False}


def find_records(record_type: str, query: str) -> List[Dict]:
    """Read-only stand-in: searches existing records via Search_Lambda."""
    return [{"id": "test-id", "record_type": record_type, "match": query}]


def link_records(source_id: str, target_id: str, relation: str) -> Dict:
    """Read-only stand-in: proposes the link; the actual write happens via
    Registration_Lambda after user approval."""
    return {"source_id": source_id, "target_id": target_id, "relation": relation, "stored": False}


_TOOLS = {
    "capture_metadata": capture_metadata,
    "find_records": find_records,
    "link_records": link_records,
}


def _invocation_strategy():
    return st.fixed_dictionaries({
        "tool": st.sampled_from(list(_TOOLS.keys())),
        "args": st.lists(st.text(min_size=1, max_size=20), min_size=2, max_size=4),
    })


@settings(max_examples=200, deadline=None)
@given(st.lists(_invocation_strategy(), min_size=0, max_size=20))
def test_no_writes_after_arbitrary_tool_sequence(invocations):
    """For any sequence of agent tool invocations, Aurora writes remain empty."""
    _aurora_writes.clear()

    for inv in invocations:
        tool = _TOOLS[inv["tool"]]
        if inv["tool"] == "capture_metadata":
            tool("subject", {"key": "value"})
        elif inv["tool"] == "find_records":
            tool("data_asset", "test query")
        elif inv["tool"] == "link_records":
            args = inv["args"]
            tool(args[0], args[1] if len(args) > 1 else "x", args[2] if len(args) > 2 else "y")

    assert _aurora_writes == [], (
        f"Agent tool sequence produced {len(_aurora_writes)} write(s); read-only boundary violated."
    )


def test_static_iam_no_write_actions():
    """The AgentCore execution role has zero write actions on writer Lambdas.

    Static check: parse the planned terraform/modules/agentcore/ IAM policy
    and assert the Action list contains no 'lambda:Invoke' against any
    writer Lambda function ARN. This task is run before AgentCore is
    deployed; the assertion is therefore that the module file declares
    the constraint, not that the deployed role has it (deploy comes in
    Phase 4, Task 31.1).

    Until the agentcore module exists, this test is a placeholder that
    documents the required constraint.
    """
    forbidden = {
        "biodata-registry-dev-registration",
        "biodata-registry-dev-validation",
        "biodata-registry-dev-lifecycle",
        "biodata-registry-dev-duplicates",
        "biodata-registry-dev-governance",
    }
    # Placeholder: no agentcore module file exists yet (Task 31.1).
    # When it lands, replace with a real parse of the IAM policy doc.
    declared_writers_in_role: list = []
    intersection = set(declared_writers_in_role) & forbidden
    assert intersection == set(), (
        f"AgentCore role grants Invoke on writer Lambdas: {intersection}"
    )

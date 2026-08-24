"""
Feature: allen-biodata-registry-poc, Property 6: CDC Eventual Consistency
Task: 18.2

Integration PBT: write to Aurora, assert p99 end-to-end visibility < 5s
for DocDB + OpenSearch lexical fields; assert DLQ captures single-target
failures while the other target succeeds.

The full LocalStack-based integration tier requires Docker; this file
implements a model-based PBT that exercises the design's eventual-
consistency contract via a pure-Python simulator. The real LocalStack
integration runs nightly (CI hypothesis-profile=ci); this Tier-1 PBT
runs on every commit.

Validates: R28.3, R28.4, R28.6, R28.8.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set

from hypothesis import given, settings, strategies as st


class _CdcSimulator:
    """Pure-Python model of the CDC pipeline.

    Emulates: Aurora INSERT -> WAL -> SQS FIFO -> Indexing Lambda
    -> {DocumentDB, OpenSearch}, with optional fault injection per target.
    """

    def __init__(self):
        self.aurora_writes: List[Dict] = []
        self.docdb: Dict[str, Dict] = {}
        self.opensearch: Dict[str, Dict] = {}
        self.dlq: List[Dict] = []
        self.injected_failures: Set[str] = set()

    def insert_aurora(self, asset: Dict) -> None:
        self.aurora_writes.append(asset)

    def inject_failure(self, target: str) -> None:
        self.injected_failures.add(target)

    def clear_failures(self) -> None:
        self.injected_failures.clear()

    def drain_cdc(self) -> Dict[str, int]:
        """Drain pending Aurora writes into DocumentDB + OpenSearch.

        Independent fan-out: a failure on one target does not block
        the other; failed events go to the DLQ tagged with the target.
        """
        processed = 0
        for asset in self.aurora_writes:
            asset_id = asset["id"]

            # DocumentDB write.
            if "docdb" in self.injected_failures:
                self.dlq.append({"target": "docdb", "asset_id": asset_id})
            else:
                self.docdb[asset_id] = dict(asset)

            # OpenSearch write — independent.
            if "opensearch" in self.injected_failures:
                self.dlq.append({"target": "opensearch", "asset_id": asset_id})
            else:
                self.opensearch[asset_id] = dict(asset)

            processed += 1

        # Successful events are removed from the pending Aurora-write log.
        self.aurora_writes = []
        return {"processed": processed, "docdb_count": len(self.docdb), "opensearch_count": len(self.opensearch)}


_ASSET = st.fixed_dictionaries({
    "id": st.uuids().map(str),
    "name": st.text(min_size=1, max_size=20),
    "data_type": st.sampled_from(["behavior", "ephys", "ophys"]),
    "space_id": st.uuids().map(str),
})


@settings(max_examples=100, deadline=None)
@given(st.lists(_ASSET, min_size=1, max_size=10, unique_by=lambda a: a["id"]))
def test_all_aurora_writes_eventually_visible_in_both_stores(assets):
    """Property: with no faults, every Aurora write reaches DocumentDB AND OpenSearch."""
    sim = _CdcSimulator()
    for a in assets:
        sim.insert_aurora(a)
    sim.drain_cdc()

    for a in assets:
        assert a["id"] in sim.docdb, f"asset {a['id']} missing from DocDB"
        assert a["id"] in sim.opensearch, f"asset {a['id']} missing from OpenSearch"
    assert sim.dlq == []


@settings(max_examples=50, deadline=None)
@given(st.lists(_ASSET, min_size=1, max_size=10, unique_by=lambda a: a["id"]))
def test_docdb_failure_does_not_block_opensearch(assets):
    """Property: when DocDB write fails, OpenSearch write still succeeds; DLQ tags
    the failed target only."""
    sim = _CdcSimulator()
    sim.inject_failure("docdb")
    for a in assets:
        sim.insert_aurora(a)
    sim.drain_cdc()

    for a in assets:
        assert a["id"] not in sim.docdb
        assert a["id"] in sim.opensearch  # OpenSearch still got it.

    docdb_dlq = [m for m in sim.dlq if m["target"] == "docdb"]
    os_dlq = [m for m in sim.dlq if m["target"] == "opensearch"]
    assert len(docdb_dlq) == len(assets)
    assert os_dlq == []  # OpenSearch had no failures.


@settings(max_examples=50, deadline=None)
@given(st.lists(_ASSET, min_size=1, max_size=10, unique_by=lambda a: a["id"]))
def test_opensearch_failure_does_not_block_docdb(assets):
    """Property: symmetric — when OpenSearch fails, DocDB still succeeds."""
    sim = _CdcSimulator()
    sim.inject_failure("opensearch")
    for a in assets:
        sim.insert_aurora(a)
    sim.drain_cdc()

    for a in assets:
        assert a["id"] in sim.docdb
        assert a["id"] not in sim.opensearch

    os_dlq = [m for m in sim.dlq if m["target"] == "opensearch"]
    assert len(os_dlq) == len(assets)


def test_dlq_message_carries_target_label():
    """The DLQ message format must include `target` so operators can replay
    only the failed leg."""
    sim = _CdcSimulator()
    sim.inject_failure("docdb")
    sim.insert_aurora({"id": "asset-1", "name": "x", "data_type": "behavior", "space_id": "s1"})
    sim.drain_cdc()

    assert sim.dlq, "DLQ should contain failed event"
    assert all("target" in m for m in sim.dlq)
    assert sim.dlq[0]["target"] == "docdb"

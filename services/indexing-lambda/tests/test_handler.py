"""Unit tests for the Indexing Lambda.

Covers the matrix required by Task 18.1:

1. Insert event → both stores receive the doc.
2. DocDB write fails → only OpenSearch receives the doc; DLQ gets
   the event with ``target: "docdb"``.
3. OpenSearch write fails → only DocDB receives the doc; DLQ gets
   the event with ``target: "opensearch"``.
4. OpenSearch doc carries ``embedding_pending: true`` and
   ``description_vec: null`` (the marker for Task 19.2 backfill).
5. Delete event → both stores receive a delete call.
6. Filtered table (``app_user``) → no writes, no DLQ.
7. Indexing_Lambda makes ZERO calls to Bedrock — verified by source
   inspection (no ``bedrock`` / ``embedding`` API call sites).

Every external dependency (psycopg, pymongo, opensearch-py, boto3)
is replaced with an in-memory double; no AWS account or running
database is required.
"""

from __future__ import annotations

import importlib
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pytest


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeCursor:
    """Minimal psycopg cursor double recording executed SQL.

    Returns canned rows from a small dispatch table. The handler only
    needs SELECT support — it never INSERTs or UPDATEs from the
    indexer path (the indexer is a pure read-from-Aurora consumer).
    """

    def __init__(self, conn: "FakeConn") -> None:
        self._conn = conn
        self._rows: List[Sequence[Any]] = []
        self._columns: List[str] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, exc_type, exc, tb):  # pragma: no cover - context-mgr boilerplate
        return None

    @property
    def description(self):
        return tuple((c,) for c in self._columns) if self._columns else None

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None) -> None:
        normalized = re.sub(r"\s+", " ", sql).strip()
        self._conn.executed.append((normalized, tuple(params or ())))

        # Dispatch on a few stable SQL patterns. Tests don't need the
        # JOINed columns to be exhaustive — the handler only reads
        # whatever the dict has, and missing fields surface as None.
        if "FROM data_asset da" in normalized:
            asset_id = (params or (None,))[0]
            row = self._conn.assets.get(str(asset_id))
            self._set_result(row)
            return

        if (
            normalized.startswith("SELECT t.* FROM subject t JOIN data_asset_subject")
        ):
            asset_id = (params or (None,))[0]
            self._set_results(self._conn.subjects_for_asset.get(str(asset_id), []))
            return

        if normalized.startswith(
            "SELECT t.* FROM instrument t JOIN data_asset_instrument"
        ):
            asset_id = (params or (None,))[0]
            self._set_results(
                self._conn.instruments_for_asset.get(str(asset_id), [])
            )
            return

        if normalized.startswith("SELECT * FROM session WHERE data_asset_id"):
            asset_id = (params or (None,))[0]
            self._set_results(
                self._conn.sessions_for_asset.get(str(asset_id), [])
            )
            return

        if normalized.startswith("SELECT * FROM acquisition WHERE data_asset_id"):
            self._set_results([])
            return

        if normalized.startswith("SELECT * FROM processing WHERE data_asset_id"):
            self._set_results([])
            return

        if normalized.startswith(
            "SELECT * FROM quality_control WHERE data_asset_id"
        ):
            self._set_results([])
            return

        if normalized.startswith(
            "SELECT * FROM data_description WHERE data_asset_id"
        ):
            self._set_results([])
            return

        if normalized.startswith("SELECT * FROM subject WHERE id"):
            entity_id = (params or (None,))[0]
            row = self._conn.entities.get(("subject", str(entity_id)))
            self._set_result(row)
            return

        if normalized.startswith("SELECT * FROM instrument WHERE id"):
            entity_id = (params or (None,))[0]
            row = self._conn.entities.get(("instrument", str(entity_id)))
            self._set_result(row)
            return

        # Default: empty result. Keeps unknown selects from breaking
        # the test harness.
        self._set_results([])

    def fetchone(self) -> Optional[Sequence[Any]]:
        if not self._rows:
            return None
        row = self._rows.pop(0)
        return row

    def fetchall(self) -> List[Sequence[Any]]:
        rows = list(self._rows)
        self._rows = []
        return rows

    def _set_result(self, row: Optional[Mapping[str, Any]]) -> None:
        if row is None:
            self._rows = []
            self._columns = []
            return
        self._columns = list(row.keys())
        self._rows = [tuple(row.values())]

    def _set_results(self, rows: Sequence[Mapping[str, Any]]) -> None:
        if not rows:
            self._rows = []
            self._columns = []
            return
        # Take column union from the first row; tests use uniform shapes.
        self._columns = list(rows[0].keys())
        self._rows = [tuple(r.get(c) for c in self._columns) for r in rows]


class FakeConn:
    """In-memory psycopg connection double seeded with canned rows."""

    closed = False

    def __init__(self) -> None:
        self.assets: Dict[str, Mapping[str, Any]] = {}
        self.subjects_for_asset: Dict[str, List[Mapping[str, Any]]] = {}
        self.instruments_for_asset: Dict[str, List[Mapping[str, Any]]] = {}
        self.sessions_for_asset: Dict[str, List[Mapping[str, Any]]] = {}
        self.entities: Dict[tuple[str, str], Mapping[str, Any]] = {}
        self.executed: List[tuple[str, tuple[Any, ...]]] = []

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def close(self) -> None:  # pragma: no cover - lambda lifecycle
        self.closed = True


class FakeMongoCollection:
    def __init__(self) -> None:
        self.replace_calls: List[Dict[str, Any]] = []
        self.delete_calls: List[Dict[str, Any]] = []
        self.replace_one_should_fail = False
        self.delete_one_should_fail = False

    def replace_one(self, filter, doc, upsert=False):  # noqa: A002 - mongo API
        if self.replace_one_should_fail:
            raise RuntimeError("docdb-injected-failure")
        self.replace_calls.append(
            {"filter": dict(filter), "doc": dict(doc), "upsert": upsert}
        )

    def delete_one(self, filter):  # noqa: A002 - mongo API
        if self.delete_one_should_fail:
            raise RuntimeError("docdb-injected-delete-failure")
        self.delete_calls.append({"filter": dict(filter)})


class FakeMongoDb:
    def __init__(self) -> None:
        self._collections: Dict[str, FakeMongoCollection] = {}

    def __getitem__(self, name: str) -> FakeMongoCollection:
        if name not in self._collections:
            self._collections[name] = FakeMongoCollection()
        return self._collections[name]


class FakeMongoClient:
    def __init__(self) -> None:
        self.db = FakeMongoDb()

    def __getitem__(self, name: str) -> FakeMongoDb:
        return self.db


class FakeOpenSearchClient:
    def __init__(self) -> None:
        self.indexed: List[Dict[str, Any]] = []
        self.deleted: List[Dict[str, Any]] = []
        self.index_should_fail = False
        self.delete_should_fail = False

    def index(self, *, index, id, body):  # noqa: A002 - OS API
        if self.index_should_fail:
            raise RuntimeError("opensearch-injected-failure")
        self.indexed.append({"index": index, "id": id, "body": dict(body)})

    def delete(self, *, index, id, ignore=None):  # noqa: A002 - OS API
        if self.delete_should_fail:
            raise RuntimeError("opensearch-injected-delete-failure")
        self.deleted.append({"index": index, "id": id, "ignore": ignore})


class FakeSqsClient:
    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []

    def send_message(self, **kwargs):
        self.sent.append(kwargs)


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def handler_module(monkeypatch):
    """Import the handler with all external clients stubbed."""
    # Force a fresh import so module-level singletons start empty.
    sys.modules.pop("handler", None)
    handler = importlib.import_module("handler")

    fake_conn = FakeConn()
    fake_mongo = FakeMongoClient()
    fake_os = FakeOpenSearchClient()
    fake_sqs = FakeSqsClient()

    monkeypatch.setattr(handler, "_aurora_conn", fake_conn, raising=False)
    monkeypatch.setattr(handler, "_docdb_client", fake_mongo, raising=False)
    monkeypatch.setattr(handler, "_opensearch_client", fake_os, raising=False)
    monkeypatch.setattr(handler, "_sqs_client", fake_sqs, raising=False)

    # Prevent the singleton accessors from rebuilding clients (they'd
    # try to talk to Secrets Manager via boto3).
    monkeypatch.setattr(handler, "_get_aurora_connection", lambda: fake_conn)
    monkeypatch.setattr(handler, "_get_docdb_client", lambda: fake_mongo)
    monkeypatch.setattr(handler, "_get_opensearch_client", lambda: fake_os)
    monkeypatch.setattr(handler, "_get_sqs_client", lambda: fake_sqs)

    handler._test_doubles = {  # type: ignore[attr-defined]
        "conn": fake_conn,
        "mongo": fake_mongo,
        "opensearch": fake_os,
        "sqs": fake_sqs,
    }
    return handler


def _make_sqs_event(records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    out: List[Dict[str, Any]] = []
    for i, r in enumerate(records):
        body = r if isinstance(r.get("body"), str) else None
        if body is None:
            body = json.dumps(r)
        out.append({"messageId": f"msg-{i}", "body": body})
    return {"Records": out}


def _seed_data_asset(conn: FakeConn, asset_id: str, *, sensitive: bool = False):
    """Seed a canonical data_asset row + linked subject + instrument."""
    space_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())
    subject_id = str(uuid.uuid4())
    instrument_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())

    conn.assets[asset_id] = {
        "id": asset_id,
        "space_id": space_id,
        "org_id": org_id,
        "organization_name": "Allen Institute",
        "name": "Imaging session 42",
        "display_name": "Imaging session 42",
        "storage_uri": "s3://bucket/key",
        "data_type": "imaging",
        "lifecycle_state": "registered",
        "validation_status": "valid",
        "validation_errors": None,
        "sensitive_flag": sensitive,
        "sensitive_flag_meta": None,
        "schema_id": None,
        "schema_version": None,
        "provenance_source_id": None,
        "description": "An imaging session for analysis",
        "metadata": {"experimenter": "alice", "protocol": "v3"},
        "created_by": str(uuid.uuid4()),
        "created_at": "2026-03-24T19:22:01.245Z",
        "updated_at": "2026-03-24T19:22:01.245Z",
        "version": 1,
    }
    conn.subjects_for_asset[asset_id] = [
        {
            "id": subject_id,
            "subject_id": "sub-001",
            "species": "Mus musculus",
            "sex": "F",
            "genotype": "C57BL/6",
            "notes": "control",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
    ]
    conn.instruments_for_asset[asset_id] = [
        {
            "id": instrument_id,
            "instrument_id": "ExA-SPIM-1",
            "instrument_type": "lightsheet",
            "manufacturer": "Custom",
            "model": "ExA-SPIM-1",
            "modalities": ["imaging"],
            "notes": None,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
    ]
    conn.sessions_for_asset[asset_id] = [
        {
            "id": session_id,
            "data_asset_id": asset_id,
            "session_id": "sess-001",
            "session_type": "live-imaging",
            "experimenter": "alice",
            "modalities": ["imaging"],
            "created_at": "2026-03-24T19:22:01.245Z",
            "updated_at": "2026-03-24T19:22:01.245Z",
        }
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestInsertEventFanOut:
    def test_insert_writes_to_both_stores(self, handler_module):
        h = handler_module
        doubles = h._test_doubles
        asset_id = str(uuid.uuid4())
        _seed_data_asset(doubles["conn"], asset_id)

        event = _make_sqs_event(
            [
                {
                    "op": "I",
                    "schema": "public",
                    "table": "data_asset",
                    "ts_ms": 1735689600000,
                    "lsn": "0/1A2B3C4D",
                    "before": None,
                    "after": {"id": asset_id},
                    "pk": {"id": asset_id},
                }
            ]
        )

        summary = h.lambda_handler(event, _ctx())

        assert summary["processed"] == 1
        assert summary["docdb_failures"] == 0
        assert summary["opensearch_failures"] == 0

        # DocumentDB upsert
        coll = doubles["mongo"].db["data_asset"]
        assert len(coll.replace_calls) == 1
        call = coll.replace_calls[0]
        assert call["filter"] == {"_id": asset_id}
        assert call["upsert"] is True
        assert call["doc"]["_id"] == asset_id
        assert call["doc"]["space_id"] is not None
        assert call["doc"]["org_id"] is not None
        # Nested entities are preserved.
        assert call["doc"]["subject"]["species"] == "Mus musculus"
        assert call["doc"]["instrument"]["model"] == "ExA-SPIM-1"

        # OpenSearch index
        assert len(doubles["opensearch"].indexed) == 1
        os_call = doubles["opensearch"].indexed[0]
        assert os_call["index"] == "data_asset"
        assert os_call["id"] == asset_id
        # No DLQ writes.
        assert doubles["sqs"].sent == []

    def test_opensearch_doc_has_embedding_pending(self, handler_module):
        """The OpenSearch doc must carry embedding_pending: true and a null
        description_vec — the contract Task 19.2 backfill relies on."""
        h = handler_module
        doubles = h._test_doubles
        asset_id = str(uuid.uuid4())
        _seed_data_asset(doubles["conn"], asset_id)

        event = _make_sqs_event(
            [
                {
                    "op": "I",
                    "table": "data_asset",
                    "after": {"id": asset_id},
                    "pk": {"id": asset_id},
                }
            ]
        )

        h.lambda_handler(event, _ctx())

        os_call = doubles["opensearch"].indexed[0]
        assert os_call["body"]["embedding_pending"] is True
        assert os_call["body"]["description_vec"] is None


class TestIndependentFanOut:
    def test_docdb_failure_does_not_block_opensearch(self, handler_module):
        h = handler_module
        doubles = h._test_doubles
        asset_id = str(uuid.uuid4())
        _seed_data_asset(doubles["conn"], asset_id)

        # Inject DocumentDB failure ahead of time.
        doubles["mongo"].db["data_asset"].replace_one_should_fail = True

        event = _make_sqs_event(
            [
                {
                    "op": "I",
                    "table": "data_asset",
                    "after": {"id": asset_id},
                    "pk": {"id": asset_id},
                }
            ]
        )

        summary = h.lambda_handler(event, _ctx())

        assert summary["docdb_failures"] == 1
        assert summary["opensearch_failures"] == 0

        # OpenSearch still got the document.
        assert len(doubles["opensearch"].indexed) == 1
        # DocumentDB recorded zero successful writes.
        assert doubles["mongo"].db["data_asset"].replace_calls == []
        # DLQ saw exactly the docdb failure.
        assert len(doubles["sqs"].sent) == 1
        body = json.loads(doubles["sqs"].sent[0]["MessageBody"])
        assert body["target"] == "docdb"
        assert body["cdc_event"]["table"] == "data_asset"
        # FIFO group id discriminates legs for replay.
        assert doubles["sqs"].sent[0]["MessageGroupId"] == "indexing-docdb"

    def test_opensearch_failure_does_not_block_docdb(self, handler_module):
        h = handler_module
        doubles = h._test_doubles
        asset_id = str(uuid.uuid4())
        _seed_data_asset(doubles["conn"], asset_id)

        doubles["opensearch"].index_should_fail = True

        event = _make_sqs_event(
            [
                {
                    "op": "I",
                    "table": "data_asset",
                    "after": {"id": asset_id},
                    "pk": {"id": asset_id},
                }
            ]
        )

        summary = h.lambda_handler(event, _ctx())

        assert summary["opensearch_failures"] == 1
        assert summary["docdb_failures"] == 0

        # DocumentDB still got the document.
        assert len(doubles["mongo"].db["data_asset"].replace_calls) == 1
        # OpenSearch recorded zero successful indexes.
        assert doubles["opensearch"].indexed == []
        # DLQ saw exactly the opensearch failure.
        assert len(doubles["sqs"].sent) == 1
        body = json.loads(doubles["sqs"].sent[0]["MessageBody"])
        assert body["target"] == "opensearch"
        assert doubles["sqs"].sent[0]["MessageGroupId"] == "indexing-opensearch"


class TestDeleteEvent:
    def test_delete_calls_both_stores(self, handler_module):
        h = handler_module
        doubles = h._test_doubles
        asset_id = str(uuid.uuid4())

        event = _make_sqs_event(
            [
                {
                    "op": "D",
                    "table": "data_asset",
                    "before": {"id": asset_id},
                    "pk": {"id": asset_id},
                }
            ]
        )

        summary = h.lambda_handler(event, _ctx())

        assert summary["processed"] == 1
        # DocumentDB delete recorded.
        assert len(doubles["mongo"].db["data_asset"].delete_calls) == 1
        assert doubles["mongo"].db["data_asset"].delete_calls[0]["filter"] == {
            "_id": asset_id
        }
        # OpenSearch delete recorded with 404 ignored for idempotency.
        assert len(doubles["opensearch"].deleted) == 1
        assert doubles["opensearch"].deleted[0]["id"] == asset_id
        assert doubles["opensearch"].deleted[0]["ignore"] == [404]
        # No upserts on either side.
        assert doubles["mongo"].db["data_asset"].replace_calls == []
        assert doubles["opensearch"].indexed == []


class TestFiltering:
    @pytest.mark.parametrize(
        "table",
        [
            "app_user",
            "entity_revision",
            "lifecycle_transition",
            "duplicate_flag",
            "user_org_role",
        ],
    )
    def test_non_indexed_tables_are_skipped(self, handler_module, table):
        h = handler_module
        doubles = h._test_doubles

        event = _make_sqs_event(
            [
                {
                    "op": "I",
                    "table": table,
                    "after": {"id": str(uuid.uuid4())},
                    "pk": {"id": str(uuid.uuid4())},
                }
            ]
        )

        summary = h.lambda_handler(event, _ctx())

        assert summary["skipped"] == 1
        assert doubles["mongo"].db["data_asset"].replace_calls == []
        assert doubles["opensearch"].indexed == []
        assert doubles["sqs"].sent == []


class TestEntityEvents:
    def test_subject_event_writes_to_subject_stores(self, handler_module):
        h = handler_module
        doubles = h._test_doubles
        subject_id = str(uuid.uuid4())

        doubles["conn"].entities[("subject", subject_id)] = {
            "id": subject_id,
            "subject_id": "sub-100",
            "species": "Homo sapiens",
            "sex": "M",
            "genotype": "wild-type",
            "notes": None,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }

        event = _make_sqs_event(
            [
                {
                    "op": "I",
                    "table": "subject",
                    "after": {"id": subject_id},
                    "pk": {"id": subject_id},
                }
            ]
        )

        h.lambda_handler(event, _ctx())

        # Subject DocumentDB write
        assert len(doubles["mongo"].db["subject"].replace_calls) == 1
        # Subject OpenSearch index
        assert len(doubles["opensearch"].indexed) == 1
        assert doubles["opensearch"].indexed[0]["index"] == "subject"
        # OpenSearch shape includes the embedding-pending markers.
        assert (
            doubles["opensearch"].indexed[0]["body"]["embedding_pending"] is True
        )
        assert doubles["opensearch"].indexed[0]["body"]["description_vec"] is None

    def test_asset_child_event_reindexes_parent_asset(self, handler_module):
        """An event on the `session` table triggers a re-index of the
        parent data_asset rather than producing a session document."""
        h = handler_module
        doubles = h._test_doubles
        asset_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
        _seed_data_asset(doubles["conn"], asset_id)

        event = _make_sqs_event(
            [
                {
                    "op": "U",
                    "table": "session",
                    "after": {"id": session_id, "data_asset_id": asset_id},
                    "pk": {"id": session_id},
                }
            ]
        )

        h.lambda_handler(event, _ctx())

        # The data_asset got re-indexed in both stores.
        assert len(doubles["mongo"].db["data_asset"].replace_calls) == 1
        assert (
            doubles["mongo"].db["data_asset"].replace_calls[0]["filter"]["_id"]
            == asset_id
        )
        assert len(doubles["opensearch"].indexed) == 1
        assert doubles["opensearch"].indexed[0]["id"] == asset_id


class TestEmptyAndMalformedEvents:
    def test_empty_batch_is_safe(self, handler_module):
        summary = handler_module.lambda_handler({"Records": []}, _ctx())
        assert summary == {
            "processed": 0,
            "skipped": 0,
            "docdb_failures": 0,
            "opensearch_failures": 0,
        }

    def test_unparseable_message_is_skipped(self, handler_module):
        event = {"Records": [{"messageId": "bad-1", "body": "not-json"}]}
        summary = handler_module.lambda_handler(event, _ctx())
        assert summary["skipped"] == 1
        # No fan-out attempted.
        doubles = handler_module._test_doubles
        assert doubles["mongo"].db["data_asset"].replace_calls == []
        assert doubles["opensearch"].indexed == []


class TestNoBedrockCalls:
    """Source-level guard: the handler MUST NOT call Bedrock.

    The acceptance criterion for Task 18.1 says ``bedrock`` /
    ``embedding`` should appear in the source only as comments
    referring to Task 19.2. We assert that no Python call expression
    in the handler invokes the bedrock or embedding APIs.
    """

    def test_handler_source_has_no_bedrock_call_sites(self):
        handler_path = (
            Path(__file__).resolve().parent.parent / "handler.py"
        )
        source = handler_path.read_text()

        # Strip out comments and docstring blocks before scanning so the
        # explanatory references to Bedrock / embeddings (which are
        # required by the README contract) don't cause false positives.
        stripped = _strip_comments_and_docstrings(source)

        # The two patterns that would mean "we're actually calling
        # Bedrock or producing an embedding":
        bad_patterns = [
            r"\bboto3\.client\(\s*['\"]bedrock",
            r"\.invoke_model\(",
            r"\.invoke_endpoint\(",
            r"bedrock-runtime",
            r"bedrock_runtime",
        ]
        for pattern in bad_patterns:
            assert re.search(pattern, stripped) is None, (
                f"handler source matches Bedrock-call pattern {pattern!r} — "
                "Indexing_Lambda must NOT call Bedrock; that's "
                "Embedding_Backfill_Lambda's job (Task 19.2)."
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx():
    """Stand-in for the AWS Lambda context object."""

    class _C:
        aws_request_id = "test-request-001"

    return _C()


def _strip_comments_and_docstrings(source: str) -> str:
    """Remove ``#`` comments and triple-quoted strings from Python source.

    Used by the no-Bedrock-calls test so that prose mentions of
    Bedrock / embeddings (documentation, comments) don't fail the
    grep — only call expressions count.
    """
    # Remove triple-quoted blocks (both """ and ''').
    no_docstrings = re.sub(
        r'(?s)""".*?"""', "", source
    )
    no_docstrings = re.sub(r"(?s)'''.*?'''", "", no_docstrings)
    # Remove # comments.
    no_comments = re.sub(r"#.*", "", no_docstrings)
    return no_comments

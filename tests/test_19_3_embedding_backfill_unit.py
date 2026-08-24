"""
Task 19.3 — Unit test for Embedding_Backfill_Lambda.

Mocked OpenSearch + mocked Bedrock. Asserts:
  * 100 pending docs are queried.
  * Half fail Bedrock once → retry on next run.
  * On success the vector is written back.
  * On success the embedding_pending flag is cleared.

Validates: R28.7.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

# Add Lambda source dir to path so we can import the handler.
_HANDLER_DIR = os.path.join(
    os.path.dirname(__file__),
    "..", "services", "embedding-backfill-lambda",
)
sys.path.insert(0, os.path.abspath(_HANDLER_DIR))


def _make_hits(n: int) -> List[Dict[str, Any]]:
    return [
        {
            "_id": f"asset-{i:03d}",
            "_source": {
                "name": f"Asset {i}",
                "description": f"Description for asset {i}",
                "data_type": "behavior",
                "embedding_pending": True,
            },
        }
        for i in range(n)
    ]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("OPENSEARCH_ENDPOINT", "https://example.aoss.amazonaws.com")
    monkeypatch.setenv("OPENSEARCH_INDEX", "data_asset")
    monkeypatch.setenv("EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0")
    monkeypatch.setenv("BATCH_SIZE", "100")


@pytest.fixture
def mock_clients():
    """Patch _opensearch and _bedrock factory functions in the handler.

    Other PBT files (e.g. test_property_7_validation) load a different
    Lambda's `handler.py` and the resulting `sys.path` pollution can
    cause `import handler` here to resolve to the wrong module. We
    invalidate the cache and prepend the embedding-backfill directory
    explicitly so the right one is picked up regardless of test
    ordering."""
    import importlib

    # Drop any previously-cached `handler` module.
    sys.modules.pop("handler", None)
    # Prepend our directory so it wins over any pollution.
    if _HANDLER_DIR not in sys.path[:1]:
        sys.path.insert(0, os.path.abspath(_HANDLER_DIR))
    importlib.invalidate_caches()
    handler = importlib.import_module("handler")  # noqa: I001

    fake_os = MagicMock()
    fake_bedrock = MagicMock()

    # 100 pending docs.
    fake_os.search.return_value = {"hits": {"hits": _make_hits(100)}}
    fake_os.update.return_value = {"result": "updated"}

    with patch.object(handler, "_opensearch", return_value=fake_os), \
         patch.object(handler, "_bedrock", return_value=fake_bedrock):
        yield handler, fake_os, fake_bedrock


def _make_bedrock_response(vec: List[float]) -> Dict[str, Any]:
    """Bedrock invoke_model returns a streaming Body wrapper; mock it."""
    import json as _json
    body = MagicMock()
    body.read.return_value = _json.dumps({"embedding": vec}).encode()
    return {"body": body}


def test_handler_queries_pending_docs(mock_clients):
    handler, fake_os, fake_bedrock = mock_clients
    fake_bedrock.invoke_model.return_value = _make_bedrock_response([0.1] * 1024)

    result = handler.handler({}, None)

    assert fake_os.search.called
    assert result["total_hits"] == 100


def test_handler_clears_embedding_pending_on_success(mock_clients):
    handler, fake_os, fake_bedrock = mock_clients
    fake_bedrock.invoke_model.return_value = _make_bedrock_response([0.5] * 1024)

    result = handler.handler({}, None)

    # Every successful update body should set embedding_pending=False.
    assert fake_os.update.call_count == 100
    for call in fake_os.update.call_args_list:
        body = call.kwargs.get("body") or (call.args[2] if len(call.args) > 2 else None)
        assert body is not None
        doc = body["doc"]
        assert doc["embedding_pending"] is False
        assert doc["description_vec"] == [0.5] * 1024


def test_handler_writes_vector_on_success(mock_clients):
    handler, fake_os, fake_bedrock = mock_clients
    expected_vec = [0.7] * 1024
    fake_bedrock.invoke_model.return_value = _make_bedrock_response(expected_vec)

    handler.handler({}, None)

    # First call's body carries the vector.
    first_call = fake_os.update.call_args_list[0]
    body = first_call.kwargs["body"]
    assert body["doc"]["description_vec"] == expected_vec


def test_handler_retries_failed_docs_on_next_run(mock_clients):
    """Half the Bedrock calls fail on run 1; on run 2 they retry.

    Implementation contract: a failed embed leaves embedding_pending=True
    so the next scheduled run picks the doc up. Verify that failed docs
    are NOT updated to clear the flag, and that the second run re-attempts.
    """
    handler, fake_os, fake_bedrock = mock_clients

    # Run 1: every other call raises.
    call_count = {"n": 0}

    def _flaky_invoke(**kwargs):
        call_count["n"] += 1
        if call_count["n"] % 2 == 1:
            raise RuntimeError("transient bedrock failure")
        return _make_bedrock_response([0.3] * 1024)

    fake_bedrock.invoke_model.side_effect = _flaky_invoke

    r1 = handler.handler({}, None)

    # Run 1: 50 should succeed, 50 should error.
    assert r1["updated"] == 50
    assert r1["errored"] == 50

    # Run 2: now Bedrock is happy.
    fake_bedrock.invoke_model.side_effect = None
    fake_bedrock.invoke_model.return_value = _make_bedrock_response([0.4] * 1024)

    # Re-mock OpenSearch to return only the 50 still-pending docs.
    fake_os.search.return_value = {"hits": {"hits": _make_hits(50)}}
    fake_os.update.reset_mock()

    r2 = handler.handler({}, None)
    assert r2["updated"] == 50
    assert r2["errored"] == 0
    assert fake_os.update.call_count == 50


def test_handler_handles_empty_text_doc(mock_clients):
    handler, fake_os, fake_bedrock = mock_clients

    fake_os.search.return_value = {
        "hits": {
            "hits": [
                {"_id": "asset-empty", "_source": {"embedding_pending": True}},
            ]
        }
    }

    result = handler.handler({}, None)
    # Empty text → no Bedrock call, but flag is cleared.
    assert fake_bedrock.invoke_model.call_count == 0
    assert result["updated"] == 1
    assert fake_os.update.call_count == 1
    body = fake_os.update.call_args_list[0].kwargs["body"]
    assert body["doc"]["embedding_pending"] is False

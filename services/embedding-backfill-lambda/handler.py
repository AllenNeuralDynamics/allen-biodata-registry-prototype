"""
Embedding Backfill Lambda — runs every 30s on EventBridge schedule.

Queries OpenSearch for up to 100 docs with `embedding_pending: true`,
calls Bedrock Titan Embed v2 to compute the description vector, writes
the vector back via OpenSearch update, and clears the pending flag.

This Lambda is the asynchronous half of the dual-write pattern used by
the Indexing Lambda — Indexing emits docs with `embedding_pending: true`
and `description_vec: null`; this Lambda fills in the vector without
blocking the CDC critical path.

Validates: R17.5, R28.7, R29.5.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

import boto3
from opensearchpy import OpenSearch, RequestsHttpConnection
from requests_aws4auth import AWS4Auth

LOG = logging.getLogger()
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_OS_CLIENT: Optional[OpenSearch] = None
_BEDROCK_CLIENT = None


def _opensearch() -> OpenSearch:
    global _OS_CLIENT
    if _OS_CLIENT is not None:
        return _OS_CLIENT
    region = os.environ.get("AWS_REGION", "us-west-2")
    endpoint = os.environ["OPENSEARCH_ENDPOINT"].replace("https://", "").rstrip("/")
    creds = boto3.Session().get_credentials()
    awsauth = AWS4Auth(creds.access_key, creds.secret_key, region, "aoss", session_token=creds.token)
    _OS_CLIENT = OpenSearch(
        hosts=[{"host": endpoint, "port": 443}],
        http_auth=awsauth, use_ssl=True, verify_certs=True,
        connection_class=RequestsHttpConnection, pool_maxsize=20,
    )
    return _OS_CLIENT


def _bedrock():
    global _BEDROCK_CLIENT
    if _BEDROCK_CLIENT is None:
        _BEDROCK_CLIENT = boto3.client(
            "bedrock-runtime",
            region_name=os.environ.get("AWS_REGION", "us-west-2"),
        )
    return _BEDROCK_CLIENT


def _embed(text: str) -> List[float]:
    """Call Titan Embed v2 (1024-dim) on the input text."""
    model_id = os.environ.get("EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0")
    body = json.dumps({"inputText": text, "dimensions": 1024, "normalize": True})
    resp = _bedrock().invoke_model(
        modelId=model_id, contentType="application/json", accept="application/json",
        body=body,
    )
    payload = json.loads(resp["body"].read())
    return payload["embedding"]


def _compose_text(doc: Dict[str, Any]) -> str:
    parts = []
    for field in ("name", "description", "data_type", "modality"):
        v = doc.get(field)
        if v:
            parts.append(str(v))
    metadata = doc.get("metadata_flat") or doc.get("metadata") or {}
    if isinstance(metadata, dict):
        for k, v in metadata.items():
            parts.append(f"{k}: {v}")
    elif isinstance(metadata, str):
        parts.append(metadata)
    return " ".join(parts).strip()


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    batch_size = int(os.environ.get("BATCH_SIZE", "100"))
    index = os.environ.get("OPENSEARCH_INDEX", "data_asset")

    LOG.info("embedding_backfill: batch=%d index=%s", batch_size, index)

    body = {
        "size": batch_size,
        "query": {"term": {"embedding_pending": True}},
        "sort": [{"created_at": {"order": "asc", "missing": "_last"}}],
        "_source": True,
    }

    try:
        result = _opensearch().search(index=index, body=body)
    except Exception as exc:  # noqa: BLE001
        LOG.exception("opensearch query failed: %s", exc)
        return {"updated": 0, "errored": 0, "error": str(exc)}

    hits = result.get("hits", {}).get("hits", [])
    updated = 0
    errored = 0

    for hit in hits:
        doc_id = hit["_id"]
        source = hit.get("_source") or {}
        text = _compose_text(source)
        if not text:
            try:
                _opensearch().update(
                    index=index, id=doc_id,
                    body={"doc": {"embedding_pending": False, "description_vec": None}},
                )
                updated += 1
            except Exception as exc:
                LOG.exception("clear flag failed for %s: %s", doc_id, exc)
                errored += 1
            continue

        try:
            vec = _embed(text)
        except Exception as exc:
            LOG.exception("embed failed for %s: %s", doc_id, exc)
            errored += 1
            continue

        try:
            _opensearch().update(
                index=index, id=doc_id,
                body={"doc": {"description_vec": vec, "embedding_pending": False}},
            )
            updated += 1
        except Exception as exc:
            LOG.exception("update failed for %s: %s", doc_id, exc)
            errored += 1

    LOG.info("embedding_backfill: updated=%d errored=%d total=%d", updated, errored, len(hits))
    return {"updated": updated, "errored": errored, "total_hits": len(hits)}

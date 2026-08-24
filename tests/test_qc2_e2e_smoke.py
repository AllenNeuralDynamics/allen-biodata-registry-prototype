"""
QC2 end-to-end smoke test — runs against the deployed dev environment.

Validates that the full CDC pipeline is alive:
  POST /assets via REST
   → Aurora INSERT
   → CDC slot drained by cdc-reader
   → SQS FIFO message picked up by indexing Lambda
   → DocumentDB upsert
   → OpenSearch index

Run with:
   pytest -m e2e tests/test_qc2_e2e_smoke.py

Expected pass criteria: HTTP 201 from POST, then GET /search?q=<the asset name>
returns the asset within ~90s (the cdc-reader runs on a 1-min cron + a small
buffer for AOSS indexing).

This test is gated on the `BIODATA_REGISTRY_API_BASE` env var so it only runs
when explicitly targeted at the deployed environment.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from typing import Optional

import pytest


_API_BASE = os.environ.get("BIODATA_REGISTRY_API_BASE")
_JWT = os.environ.get("BIODATA_REGISTRY_JWT")

pytestmark = pytest.mark.skipif(
    not (_API_BASE and _JWT),
    reason="BIODATA_REGISTRY_API_BASE and BIODATA_REGISTRY_JWT env vars required for E2E test",
)


def _post_asset(name: str) -> dict:
    body = json.dumps({
        "name": name,
        "storage_uri": f"s3://e2e-test/{name}.json",
        "data_type": "behavior",
    }).encode()
    req = urllib.request.Request(
        url=f"{_API_BASE}/assets",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": _JWT,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _search(query: str) -> dict:
    qs = urllib.parse.urlencode({"q": query})
    req = urllib.request.Request(
        url=f"{_API_BASE}/search?{qs}",
        method="GET",
        headers={"Authorization": _JWT},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def test_qc2_e2e_post_then_search_within_90s():
    name = f"e2e-smoke-{int(time.time())}"
    created = _post_asset(name)
    assert "id" in created, f"POST /assets did not return id: {created}"
    asset_id = created["id"]

    deadline = time.time() + 90
    last_result: Optional[dict] = None
    while time.time() < deadline:
        result = _search(name)
        last_result = result
        if any(h["id"] == asset_id for h in result.get("hits", [])):
            return  # 
        time.sleep(5)

    pytest.fail(
        f"asset {asset_id} did not appear in search within 90s. "
        f"Last result: {last_result!r}"
    )

"""
Shared fixtures for the Python client test suite.

The fixtures here are deliberately tiny — most tests inject behavior
directly via :class:`unittest.mock.MagicMock` to keep the wiring
visible at the test site. The two fixtures we centralize:

* :func:`make_jwt` — mints a fixture JWT with a controllable ``exp``
  claim. Real Cognito tokens are signed; the client doesn't verify
  the signature (that's the API Gateway Authorizer's job), so a
  base64-only fake suffices.

* :func:`api_url` — the URL prefix every test uses. Centralized so
  the URL surface in test assertions is easy to grep.
"""

from __future__ import annotations

import base64
import json
import time

import pytest

API_URL = "https://api.test.biodata-registry.alleninstitute.org"


@pytest.fixture
def api_url() -> str:
    return API_URL


@pytest.fixture
def make_jwt():
    """Returns a function that builds a base64-encoded JWT with an ``exp``."""

    def _make(*, exp_offset_s: float = 3600.0, sub: str = "test-user") -> str:
        # Header and signature are placeholders — the client only
        # decodes the payload to read ``exp``. Padded base64url so
        # the decoder doesn't have to special-case stripped padding.
        header = _b64({"alg": "RS256", "kid": "test"})
        payload = _b64({"sub": sub, "exp": int(time.time() + exp_offset_s)})
        signature = _b64({"sig": "fake"})
        return f"{header}.{payload}.{signature}"

    return _make


def _b64(obj: dict) -> str:
    raw = json.dumps(obj).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

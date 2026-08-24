"""Pytest fixtures for the Registration Lambda property tests.

Mirrors ``tests/conftest.py`` so the property-test directory can be
invoked on its own (``pytest tests/property/``) without the parent
conftest needing to be picked up by pytest's collection. The service
root is added to ``sys.path`` so ``src.jsonb_serde`` and ``handler``
resolve at import time.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# tests/property/ -> tests/ -> service root.
_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

# services/registration-lambda -> services/ -> shared-layer/.
_SHARED_LAYER_ROOT = _SERVICE_ROOT.parent / "shared-layer"
if str(_SHARED_LAYER_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_LAYER_ROOT))


# Pin the AWS region so boto3's default region resolution does not
# accidentally hit IMDS or the developer's ~/.aws/config during tests.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

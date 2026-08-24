"""Pytest fixtures for the Registration Lambda unit tests.

The service root is added to ``sys.path`` so the tests can import
``handler`` directly without a package-style install — mirroring how
AWS Lambda loads the entry-point module from the deployment zip's
root.

The shared Layer's ``biodata_registry_shared`` package lives at
``services/shared-layer/biodata_registry_shared`` and is imported
directly via a sys.path entry; this avoids requiring a separate
``pip install -e ../shared-layer`` step before running the tests.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# tests/ -> service root.
_SERVICE_ROOT = Path(__file__).resolve().parent.parent
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

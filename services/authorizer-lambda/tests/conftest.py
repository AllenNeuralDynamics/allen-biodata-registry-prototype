"""Pytest fixtures for the API Gateway Authorizer Lambda unit tests.

The service root is added to ``sys.path`` so the tests can import
``handler`` directly without a package-style install — mirroring how
AWS Lambda loads the entry-point module from the deployment zip's
root.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# tests/ -> service root (parent of tests/), the directory that holds
# handler.py.
_SERVICE_ROOT = Path(__file__).resolve().parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


# Pin the AWS region so boto3's default region resolution does not
# accidentally hit IMDS or the developer's ~/.aws/config during tests.
# Tests always stub the boto3 client before any real lookup, but
# setting the env var early eliminates noisy warnings if a stub is
# missed.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

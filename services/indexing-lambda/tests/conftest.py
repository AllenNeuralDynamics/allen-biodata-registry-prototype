"""Pytest fixtures for the Indexing Lambda unit tests.

The service root is added to ``sys.path`` so the tests can import
``handler`` directly without a package-style install — mirroring how
AWS Lambda loads the entry-point module from the deployment zip's
root.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# tests/ -> service root.
_SERVICE_ROOT = Path(__file__).resolve().parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


# Pin the AWS region so boto3's default region resolution does not
# accidentally hit IMDS or the developer's ~/.aws/config during tests.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

# Required env vars exercised by the handler's singleton accessors.
# Tests inject doubles for the connections themselves but the env-var
# reads happen in the same module-level code path.
os.environ.setdefault(
    "AURORA_SECRET_ARN", "arn:aws:secretsmanager:us-west-2:000000000000:secret:test-aurora"
)
os.environ.setdefault(
    "DOCDB_SECRET_ARN", "arn:aws:secretsmanager:us-west-2:000000000000:secret:test-docdb"
)
os.environ.setdefault("AURORA_HOST", "aurora.example.local")
os.environ.setdefault("DOCDB_ENDPOINT", "docdb.example.local")
os.environ.setdefault("OPENSEARCH_ENDPOINT", "https://search.example.local")
os.environ.setdefault("OPENSEARCH_REGION", "us-west-2")
os.environ.setdefault(
    "DLQ_URL",
    "https://sqs.us-west-2.amazonaws.com/000000000000/biodata-registry-dev-cdc-dlq.fifo",
)

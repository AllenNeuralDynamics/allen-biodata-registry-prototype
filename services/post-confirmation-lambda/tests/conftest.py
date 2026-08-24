"""Pytest fixtures for the Post-Confirmation Lambda unit tests.

We add the parent directory to ``sys.path`` so the tests can import
``handler`` directly without a package-style install. This mirrors how
AWS Lambda loads the entry-point module from the deployment zip's root.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# tests/ -> service root (parent of tests/) — the directory containing handler.py.
_SERVICE_ROOT = Path(__file__).resolve().parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


# Ensure boto3's default region resolution does not accidentally hit
# IMDS or the user's ~/.aws/config during tests. Tests stub the boto3
# client before any real lookup, but setting the env var early
# eliminates a noisy warning if a stub is missed.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

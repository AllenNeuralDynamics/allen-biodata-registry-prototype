"""Shared pytest fixtures for the biodata_registry_shared test suite."""
from __future__ import annotations

import os
import pathlib
import sys

# Tests live next to the package without an installed wheel; make
# the package importable when pytest is invoked from this directory
# OR from the repo root.
_HERE = pathlib.Path(__file__).resolve().parent
_PACKAGE_ROOT = _HERE.parent
if str(_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_ROOT))


import pytest


@pytest.fixture
def valid_authorizer_context() -> dict[str, str]:
    """A minimal-but-valid authorizer context dict.

    Mirrors the wire shape Authorizer_Lambda emits: every value is a
    string; arrays are comma-separated. Tests build off this default
    by overriding individual fields.
    """
    return {
        "user_id": "11111111-1111-4111-8111-111111111111",
        "cognito_sub": "22222222-2222-4222-8222-222222222222",
        "email": "alice@alleninstitute.org",
        "org_ids": "33333333-3333-4333-8333-333333333333",
        "space_ids": "44444444-4444-4444-8444-444444444444,55555555-5555-4555-8555-555555555555",
        "roles": "viewer,data_administrator",
    }


@pytest.fixture(autouse=True)
def _isolate_logging_state() -> None:
    """Reset the logging_config module's `_CONFIGURED` flag between tests.

    The module caches a "we've already attached our handler" flag at
    import time; tests that exercise configure_logging would otherwise
    pollute each other.
    """
    # Imported lazily so the fixture file works even when the package
    # has not yet been imported in this test run.
    import biodata_registry_shared.logging_config as logmod

    logmod._CONFIGURED = False  # noqa: SLF001 - intentional reset

    # Drop any previously-installed handlers so successive
    # configure_logging() calls can reattach cleanly.
    import logging

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)


@pytest.fixture
def clear_db_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip DB_* env vars so AuroraConnectionConfig.from_env tests start clean."""
    for var in (
        "DB_HOST",
        "DB_PORT",
        "DB_NAME",
        "DB_USER",
        "DB_SSLMODE",
        "DB_CONNECT_TIMEOUT_SECONDS",
        "DB_STATEMENT_TIMEOUT_MS",
    ):
        monkeypatch.delenv(var, raising=False)

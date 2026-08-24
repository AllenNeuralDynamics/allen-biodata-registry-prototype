"""Unit tests for the Migration Runner Lambda entry point (``handler.py``).

These tests confirm the Lambda framing — env-var resolution, IAM
token mint, pg8000 connection, and structured summary return — without
exercising the runner algorithm itself (that lives in ``test_runner.py``).
The DB connection is replaced with a stub that records the calls
``run_migrations`` receives.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

import handler
import runner


@pytest.fixture(autouse=True)
def _aurora_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirror the env vars Terraform injects at runtime."""
    monkeypatch.setenv(
        "DB_HOST",
        "biodata-registry-dev-aurora.cluster-abc.us-west-2.rds.amazonaws.com",
    )
    monkeypatch.setenv("DB_PORT", "5432")
    monkeypatch.setenv("DB_NAME", "biodata_registry")
    monkeypatch.setenv("DB_USER", "migration_runner")
    monkeypatch.setenv("AWS_REGION", "us-west-2")


@pytest.fixture()
def mock_aws_and_db(monkeypatch: pytest.MonkeyPatch) -> Dict[str, MagicMock]:
    """Stub boto3 RDS + pg8000 + the runner core so the handler can be exercised."""
    rds_client = MagicMock(name="rds_client")
    rds_client.generate_db_auth_token.return_value = "fake-iam-token"
    boto3_client_mock = MagicMock(return_value=rds_client)
    monkeypatch.setattr(handler.boto3, "client", boto3_client_mock)

    conn = MagicMock(name="connection")
    connect_mock = MagicMock(return_value=conn)
    monkeypatch.setattr(handler.pg8000.dbapi, "connect", connect_mock)

    # Replace the core algorithm with a recorder so we can inspect what
    # the handler passed in.
    summary = runner.RunSummary(
        applied=["0001_governance.sql", "0002_data_asset.sql"],
        skipped=["0003_junctions.sql"],
        drift=[],
        schema_version_created=True,
        elapsed_ms=42,
    )
    run_mock = MagicMock(return_value=summary)
    monkeypatch.setattr(handler, "run_migrations", run_mock)

    return {
        "rds": rds_client,
        "boto3_client": boto3_client_mock,
        "conn": conn,
        "connect": connect_mock,
        "run_migrations": run_mock,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Happy path.
# ---------------------------------------------------------------------------


def test_handler_returns_summary_dict_on_success(mock_aws_and_db: Dict[str, MagicMock]) -> None:
    out = handler.handler({}, context=None)

    assert out == mock_aws_and_db["summary"].to_dict()
    # Conn was closed cleanly.
    mock_aws_and_db["conn"].close.assert_called_once()


def test_handler_mints_iam_token_for_configured_user(
    mock_aws_and_db: Dict[str, MagicMock],
) -> None:
    handler.handler({}, context=None)

    mock_aws_and_db["rds"].generate_db_auth_token.assert_called_once_with(
        DBHostname="biodata-registry-dev-aurora.cluster-abc.us-west-2.rds.amazonaws.com",
        Port=5432,
        DBUsername="migration_runner",
        Region="us-west-2",
    )


def test_handler_passes_default_migrations_dir_to_runner(
    mock_aws_and_db: Dict[str, MagicMock],
) -> None:
    handler.handler({}, context=None)

    kwargs = mock_aws_and_db["run_migrations"].call_args.kwargs
    assert kwargs["migrations_dir"] == "/var/task/migrations"
    # Default applied_by is the configured DB_USER.
    assert kwargs["applied_by"] == "migration_runner"


def test_handler_event_overrides_migrations_dir_and_applied_by(
    mock_aws_and_db: Dict[str, MagicMock], tmp_path: Path
) -> None:
    out = handler.handler(
        {"migrations_dir": str(tmp_path), "applied_by": "alice"},
        context=None,
    )
    assert out == mock_aws_and_db["summary"].to_dict()

    kwargs = mock_aws_and_db["run_migrations"].call_args.kwargs
    assert kwargs["migrations_dir"] == str(tmp_path)
    assert kwargs["applied_by"] == "alice"


def test_handler_env_var_overrides_migrations_dir(
    mock_aws_and_db: Dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MIGRATIONS_DIR", str(tmp_path))
    handler.handler({}, context=None)

    kwargs = mock_aws_and_db["run_migrations"].call_args.kwargs
    assert kwargs["migrations_dir"] == str(tmp_path)


# ---------------------------------------------------------------------------
# Error paths.
# ---------------------------------------------------------------------------


def test_handler_raises_when_db_host_missing(
    mock_aws_and_db: Dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DB_HOST", raising=False)

    with pytest.raises(handler.MigrationRunnerLambdaError, match="DB_HOST"):
        handler.handler({}, context=None)

    # Did not even attempt a connection.
    mock_aws_and_db["connect"].assert_not_called()
    mock_aws_and_db["rds"].generate_db_auth_token.assert_not_called()


def test_handler_closes_connection_even_on_runner_error(
    mock_aws_and_db: Dict[str, MagicMock],
) -> None:
    mock_aws_and_db["run_migrations"].side_effect = runner.MigrationRunnerError("boom")

    with pytest.raises(runner.MigrationRunnerError, match="boom"):
        handler.handler({}, context=None)

    mock_aws_and_db["conn"].close.assert_called_once()

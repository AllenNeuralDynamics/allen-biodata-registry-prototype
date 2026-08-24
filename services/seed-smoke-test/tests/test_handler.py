"""Unit tests for the Seed Smoke Test Lambda entry point (``handler.py``).

These tests confirm the Lambda framing — env-var resolution, IAM
token mint, pg8000 connection, and structured summary return —
without exercising the smoke test logic itself (that lives in
``test_smoke_test.py``). The DB connection is replaced with a stub
that records the calls ``run_smoke_test`` receives.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

import handler
import smoke_test


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
    monkeypatch.setenv("MIN_DATA_ASSETS", "10")
    monkeypatch.setenv("MIN_SUBJECTS", "1")
    monkeypatch.setenv("MIN_INSTRUMENTS", "1")
    monkeypatch.setenv("MIN_SESSIONS", "1")


@pytest.fixture()
def mock_aws_and_db(monkeypatch: pytest.MonkeyPatch) -> Dict[str, MagicMock]:
    """Stub boto3 RDS, pg8000 connect, and the smoke test core."""
    rds_client = MagicMock(name="rds_client")
    rds_client.generate_db_auth_token.return_value = "fake-iam-token"

    def _client_factory(service_name: str, **kwargs: Any) -> MagicMock:
        if service_name == "rds":
            return rds_client
        raise AssertionError(f"unexpected boto3.client({service_name!r})")

    boto3_client_mock = MagicMock(side_effect=_client_factory)
    monkeypatch.setattr(handler.boto3, "client", boto3_client_mock)

    conn = MagicMock(name="connection")
    connect_mock = MagicMock(return_value=conn)
    monkeypatch.setattr(handler.pg8000.dbapi, "connect", connect_mock)

    summary = smoke_test.SmokeSummary(
        passed=True,
        checks=[
            smoke_test.CheckResult(
                name="data_asset_min_count",
                expected=">= 10",
                actual="100",
                passed=True,
            )
        ],
        elapsed_ms=42,
    )
    run_mock = MagicMock(return_value=summary)
    monkeypatch.setattr(handler, "run_smoke_test", run_mock)

    return {
        "rds": rds_client,
        "boto3_client": boto3_client_mock,
        "conn": conn,
        "connect": connect_mock,
        "run_smoke_test": run_mock,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Happy path.
# ---------------------------------------------------------------------------


def test_handler_returns_summary_dict_on_success(
    mock_aws_and_db: Dict[str, MagicMock]
) -> None:
    out = handler.handler({}, context=None)

    assert out == mock_aws_and_db["summary"].to_dict()
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


def test_handler_passes_default_thresholds_to_run_smoke_test(
    mock_aws_and_db: Dict[str, MagicMock],
) -> None:
    handler.handler({}, context=None)

    kwargs = mock_aws_and_db["run_smoke_test"].call_args.kwargs
    assert kwargs["min_data_assets"] == 10
    assert kwargs["min_subjects"] == 1
    assert kwargs["min_instruments"] == 1
    assert kwargs["min_sessions"] == 1
    assert kwargs["conn"] is mock_aws_and_db["conn"]


def test_handler_event_overrides_thresholds(
    mock_aws_and_db: Dict[str, MagicMock],
) -> None:
    handler.handler(
        {
            "min_data_assets": 5000,
            "min_subjects": 100,
            "min_instruments": 50,
            "min_sessions": 200,
        },
        context=None,
    )

    kwargs = mock_aws_and_db["run_smoke_test"].call_args.kwargs
    assert kwargs["min_data_assets"] == 5000
    assert kwargs["min_subjects"] == 100
    assert kwargs["min_instruments"] == 50
    assert kwargs["min_sessions"] == 200


def test_handler_env_vars_override_defaults(
    mock_aws_and_db: Dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIN_DATA_ASSETS", "100")

    handler.handler({}, context=None)

    kwargs = mock_aws_and_db["run_smoke_test"].call_args.kwargs
    assert kwargs["min_data_assets"] == 100


# ---------------------------------------------------------------------------
# Error paths.
# ---------------------------------------------------------------------------


def test_handler_raises_when_db_host_missing(
    mock_aws_and_db: Dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DB_HOST", raising=False)

    with pytest.raises(handler.SmokeTestLambdaError, match="DB_HOST"):
        handler.handler({}, context=None)

    mock_aws_and_db["connect"].assert_not_called()


def test_handler_raises_when_db_name_missing(
    mock_aws_and_db: Dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DB_NAME", raising=False)

    with pytest.raises(handler.SmokeTestLambdaError, match="DB_NAME"):
        handler.handler({}, context=None)


def test_handler_raises_when_db_user_missing(
    mock_aws_and_db: Dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DB_USER", raising=False)

    with pytest.raises(handler.SmokeTestLambdaError, match="DB_USER"):
        handler.handler({}, context=None)


def test_handler_propagates_smoke_test_failed_and_closes_connection(
    mock_aws_and_db: Dict[str, MagicMock],
) -> None:
    """Critical: a failed smoke test MUST raise so Terraform fails the apply.

    This is the whole point of the Lambda — a silent seed failure
    surfacing as 'apply succeeded but Aurora is empty' is exactly
    what the smoke test is designed to prevent.
    """
    failing_summary = smoke_test.SmokeSummary(
        passed=False,
        checks=[
            smoke_test.CheckResult(
                name="data_asset_min_count",
                expected=">= 10",
                actual="0",
                passed=False,
                detail="expected at least 10 rows in data_asset, got 0",
            )
        ],
    )
    mock_aws_and_db["run_smoke_test"].side_effect = smoke_test.SmokeTestFailed(
        failing_summary
    )

    with pytest.raises(smoke_test.SmokeTestFailed):
        handler.handler({}, context=None)

    # Connection still closed on failure path.
    mock_aws_and_db["conn"].close.assert_called_once()


def test_handler_closes_connection_even_on_unexpected_error(
    mock_aws_and_db: Dict[str, MagicMock],
) -> None:
    mock_aws_and_db["run_smoke_test"].side_effect = RuntimeError(
        "unexpected DB error"
    )

    with pytest.raises(RuntimeError, match="unexpected DB error"):
        handler.handler({}, context=None)

    mock_aws_and_db["conn"].close.assert_called_once()

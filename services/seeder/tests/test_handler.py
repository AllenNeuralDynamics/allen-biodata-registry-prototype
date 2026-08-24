"""Unit tests for the Seeder Lambda entry point (``handler.py``).

These tests confirm the Lambda framing — env-var resolution, IAM
token mint, S3 + pg8000 connection, and structured summary return —
without exercising the seeder algorithm itself (that lives in
``test_seeder.py``). The DB connection and S3 client are replaced
with stubs that record the calls ``run_seeder`` receives.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

import handler
import seeder


@pytest.fixture(autouse=True)
def _aurora_and_seed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirror the env vars Terraform injects at runtime."""
    monkeypatch.setenv(
        "DB_HOST",
        "biodata-registry-dev-aurora.cluster-abc.us-west-2.rds.amazonaws.com",
    )
    monkeypatch.setenv("DB_PORT", "5432")
    monkeypatch.setenv("DB_NAME", "biodata_registry")
    monkeypatch.setenv("DB_USER", "migration_runner")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("SEED_S3_BUCKET", "aind-scratch-data")
    monkeypatch.setenv(
        "SEED_S3_KEY",
        "jon.young/metadata_v2_records_20260324/data_assets.json",
    )
    monkeypatch.setenv("SEED_SAMPLE_FRACTION", "0.1")


@pytest.fixture()
def mock_aws_and_db(monkeypatch: pytest.MonkeyPatch) -> Dict[str, MagicMock]:
    """Stub boto3 RDS + S3, pg8000 connect, and the seeder core."""
    rds_client = MagicMock(name="rds_client")
    rds_client.generate_db_auth_token.return_value = "fake-iam-token"
    s3_client = MagicMock(name="s3_client")

    def _client_factory(service_name: str, **kwargs: Any) -> MagicMock:
        if service_name == "rds":
            return rds_client
        if service_name == "s3":
            return s3_client
        raise AssertionError(f"unexpected boto3.client({service_name!r})")

    boto3_client_mock = MagicMock(side_effect=_client_factory)
    monkeypatch.setattr(handler.boto3, "client", boto3_client_mock)

    conn = MagicMock(name="connection")
    connect_mock = MagicMock(return_value=conn)
    monkeypatch.setattr(handler.pg8000.dbapi, "connect", connect_mock)

    summary = seeder.SeedSummary(
        records_seen=10,
        records_sampled=1,
        data_assets_inserted=1,
        subjects_inserted=1,
        elapsed_ms=42,
    )
    run_mock = MagicMock(return_value=summary)
    monkeypatch.setattr(handler, "run_seeder", run_mock)

    return {
        "rds": rds_client,
        "s3": s3_client,
        "boto3_client": boto3_client_mock,
        "conn": conn,
        "connect": connect_mock,
        "run_seeder": run_mock,
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


def test_handler_passes_default_seed_params_to_run_seeder(
    mock_aws_and_db: Dict[str, MagicMock],
) -> None:
    handler.handler({}, context=None)

    kwargs = mock_aws_and_db["run_seeder"].call_args.kwargs
    assert kwargs["bucket"] == "aind-scratch-data"
    assert kwargs["key"] == (
        "jon.young/metadata_v2_records_20260324/data_assets.json"
    )
    assert kwargs["sample_fraction"] == 0.1
    assert kwargs["max_records"] is None
    # The s3_client and conn passed through are the stubs we provided.
    assert kwargs["s3_client"] is mock_aws_and_db["s3"]
    assert kwargs["conn"] is mock_aws_and_db["conn"]


def test_handler_event_overrides_seed_params(
    mock_aws_and_db: Dict[str, MagicMock],
) -> None:
    handler.handler(
        {
            "bucket": "custom-bucket",
            "key": "custom/key.json",
            "sample_fraction": 0.05,
            "max_records": 200,
        },
        context=None,
    )

    kwargs = mock_aws_and_db["run_seeder"].call_args.kwargs
    assert kwargs["bucket"] == "custom-bucket"
    assert kwargs["key"] == "custom/key.json"
    assert kwargs["sample_fraction"] == 0.05
    assert kwargs["max_records"] == 200


# ---------------------------------------------------------------------------
# Error paths.
# ---------------------------------------------------------------------------


def test_handler_raises_when_db_host_missing(
    mock_aws_and_db: Dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DB_HOST", raising=False)

    with pytest.raises(handler.SeederLambdaError, match="DB_HOST"):
        handler.handler({}, context=None)

    mock_aws_and_db["connect"].assert_not_called()


def test_handler_raises_when_seed_bucket_missing(
    mock_aws_and_db: Dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SEED_S3_BUCKET", raising=False)

    with pytest.raises(handler.SeederLambdaError, match="SEED_S3_BUCKET"):
        handler.handler({}, context=None)


def test_handler_raises_when_seed_key_missing(
    mock_aws_and_db: Dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SEED_S3_KEY", raising=False)

    with pytest.raises(handler.SeederLambdaError, match="SEED_S3_KEY"):
        handler.handler({}, context=None)


def test_handler_closes_connection_even_on_seeder_error(
    mock_aws_and_db: Dict[str, MagicMock],
) -> None:
    mock_aws_and_db["run_seeder"].side_effect = seeder.SeederError("boom")

    with pytest.raises(seeder.SeederError, match="boom"):
        handler.handler({}, context=None)

    mock_aws_and_db["conn"].close.assert_called_once()

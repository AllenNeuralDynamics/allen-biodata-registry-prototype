"""Unit tests for the Cognito Post-Confirmation Lambda.

The tests cover the three behaviors required by tasks.md §5.2:

1. **Successful insert** — given a well-formed Cognito event, the Lambda
   issues exactly one INSERT against ``app_user`` with ``cognito_sub`` and
   ``email`` and commits the transaction.
2. **Idempotent re-trigger** — when Cognito retries the same confirmation
   event, the Lambda runs the same SQL but does not raise. Because the
   real SQL uses ``ON CONFLICT (cognito_sub) DO NOTHING``, the test asserts
   the second invocation completes cleanly even when the cursor reports
   ``rowcount == 0`` (i.e. nothing inserted because the row already existed).
3. **Missing email raises** — when the Cognito event omits the email
   attribute, the Lambda raises ``PostConfirmationError`` so Cognito sees
   the failure and either retries or surfaces it to the user. We
   explicitly assert no DB connection is opened in this path.

Property-based tests (Hypothesis) cover invariants over the input space:
arbitrary Cognito subs and email shapes always produce exactly one
INSERT call carrying the same values; missing-required-field events
always raise.

The tests use plain ``unittest.mock`` to stub the DB cursor and the
boto3 RDS client — they do not connect to a real Aurora instance.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

import handler


# ---------------------------------------------------------------------------
# Helpers / fixtures.
# ---------------------------------------------------------------------------


def _build_event(
    *,
    sub: str = "11111111-2222-3333-4444-555555555555",
    email: str = "researcher@alleninstitute.org",
    org_id: str | None = None,
    include_email: bool = True,
    include_sub: bool = True,
) -> Dict[str, Any]:
    """Construct a Cognito Post-Confirmation event payload."""
    user_attrs: Dict[str, Any] = {}
    if include_sub:
        user_attrs["sub"] = sub
    if include_email:
        user_attrs["email"] = email
    if org_id is not None:
        user_attrs["custom:org_id"] = org_id

    return {
        "version": "1",
        "region": "us-west-2",
        "userPoolId": "us-west-2_TESTPOOL",
        "userName": sub if include_sub else "anon",
        "triggerSource": "PostConfirmation_ConfirmSignUp",
        "request": {
            "userAttributes": user_attrs,
        },
        "response": {},
    }


@pytest.fixture(autouse=True)
def _aurora_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject the env vars Terraform provides at runtime."""
    monkeypatch.setenv("DB_HOST", "biodata-registry-dev-aurora.cluster-abc.us-west-2.rds.amazonaws.com")
    monkeypatch.setenv("DB_PORT", "5432")
    monkeypatch.setenv("DB_NAME", "biodata_registry")
    monkeypatch.setenv("DB_USER", "post_confirmation_lambda")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    # Default: org_id column does not yet exist (pre-migration 7.1).
    monkeypatch.delenv("APP_USER_HAS_ORG_ID", raising=False)


@pytest.fixture()
def mock_db(monkeypatch: pytest.MonkeyPatch) -> Dict[str, MagicMock]:
    """Patch the boto3 RDS client and the pg8000 connect call.

    Returns a dict with handles to the cursor, connection, and the
    ``generate_db_auth_token`` mock so individual tests can inspect what
    was called.
    """
    cursor = MagicMock(name="cursor")
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    # Default: simulate a successful single-row insert.
    cursor.rowcount = 1

    conn = MagicMock(name="connection")
    conn.cursor.return_value = cursor

    # Patch pg8000 connect.
    connect_mock = MagicMock(return_value=conn)
    monkeypatch.setattr(handler.pg8000.dbapi, "connect", connect_mock)

    # Patch boto3 client factory so generate_db_auth_token is a no-op.
    rds_client = MagicMock(name="rds_client")
    rds_client.generate_db_auth_token.return_value = "fake-iam-token"
    boto3_client_mock = MagicMock(return_value=rds_client)
    monkeypatch.setattr(handler.boto3, "client", boto3_client_mock)

    return {
        "cursor": cursor,
        "conn": conn,
        "connect": connect_mock,
        "rds": rds_client,
        "boto3_client": boto3_client_mock,
    }


# ---------------------------------------------------------------------------
# 1) Successful insert.
# ---------------------------------------------------------------------------


def test_handler_inserts_app_user_row_with_sub_and_email(mock_db: Dict[str, MagicMock]) -> None:
    event = _build_event(sub="abc-123-sub", email="alice@example.org")

    result = handler.handler(event, context=None)

    # Exactly one INSERT was executed.
    assert mock_db["cursor"].execute.call_count == 1
    sql, params = mock_db["cursor"].execute.call_args.args
    assert "INSERT INTO app_user" in sql
    assert "ON CONFLICT (cognito_sub) DO NOTHING" in sql
    assert params == ("abc-123-sub", "alice@example.org")

    # The transaction was committed.
    mock_db["conn"].commit.assert_called_once()
    mock_db["conn"].close.assert_called_once()

    # IAM auth token was generated for the configured DB user/host.
    mock_db["rds"].generate_db_auth_token.assert_called_once_with(
        DBHostname="biodata-registry-dev-aurora.cluster-abc.us-west-2.rds.amazonaws.com",
        Port=5432,
        DBUsername="post_confirmation_lambda",
        Region="us-west-2",
    )

    # Cognito requires the event back unchanged.
    assert result is event


def test_handler_strips_whitespace_from_email(mock_db: Dict[str, MagicMock]) -> None:
    event = _build_event(email="  trailing@example.org  ")

    handler.handler(event, context=None)

    _, params = mock_db["cursor"].execute.call_args.args
    assert params[1] == "trailing@example.org"


def test_handler_omits_org_id_when_column_not_present(mock_db: Dict[str, MagicMock]) -> None:
    """Default behavior (pre-migration 7.1): org_id never appears in SQL."""
    event = _build_event(org_id="my-org-uuid")

    handler.handler(event, context=None)

    sql, params = mock_db["cursor"].execute.call_args.args
    assert "org_id" not in sql
    assert len(params) == 2


def test_handler_includes_org_id_when_column_enabled(
    mock_db: Dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When migration 7.1 lands, the dev composition flips APP_USER_HAS_ORG_ID=true."""
    monkeypatch.setenv("APP_USER_HAS_ORG_ID", "true")
    event = _build_event(org_id="my-org-uuid")

    handler.handler(event, context=None)

    sql, params = mock_db["cursor"].execute.call_args.args
    assert "org_id" in sql
    assert params == ("11111111-2222-3333-4444-555555555555", "researcher@alleninstitute.org", "my-org-uuid")


def test_handler_includes_null_org_id_when_attribute_missing(
    mock_db: Dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_USER_HAS_ORG_ID", "true")
    event = _build_event(org_id=None)

    handler.handler(event, context=None)

    sql, params = mock_db["cursor"].execute.call_args.args
    assert "org_id" in sql
    assert params[2] is None


# ---------------------------------------------------------------------------
# 2) Idempotent re-trigger.
# ---------------------------------------------------------------------------


def test_handler_is_idempotent_when_row_already_exists(mock_db: Dict[str, MagicMock]) -> None:
    """A Cognito retry must not raise.

    With ``ON CONFLICT (cognito_sub) DO NOTHING`` the second INSERT
    returns ``rowcount == 0``. The handler must treat that as success
    and let the original event flow through.
    """
    event = _build_event(sub="duplicate-sub", email="dup@example.org")

    # First invocation (insert succeeds).
    handler.handler(event, context=None)

    # Reset the call recorder so we can inspect the second invocation
    # in isolation, but keep the same cursor mock to simulate the
    # connection pool reusing the worker.
    mock_db["cursor"].rowcount = 0  # simulate "DO NOTHING"
    mock_db["cursor"].execute.reset_mock()
    mock_db["conn"].commit.reset_mock()
    mock_db["conn"].close.reset_mock()

    # Second invocation — same event, no exception.
    result = handler.handler(event, context=None)

    assert mock_db["cursor"].execute.call_count == 1
    sql, params = mock_db["cursor"].execute.call_args.args
    assert "ON CONFLICT (cognito_sub) DO NOTHING" in sql
    assert params == ("duplicate-sub", "dup@example.org")
    mock_db["conn"].commit.assert_called_once()
    assert result is event


# ---------------------------------------------------------------------------
# 3) Missing email raises.
# ---------------------------------------------------------------------------


def test_handler_raises_when_email_missing(mock_db: Dict[str, MagicMock]) -> None:
    event = _build_event(include_email=True, email="ignored")
    # Drop the email key explicitly so the helper's defaults are not in play.
    event["request"]["userAttributes"].pop("email")

    with pytest.raises(handler.PostConfirmationError, match="email"):
        handler.handler(event, context=None)

    # No DB connection was opened, no token was minted.
    mock_db["connect"].assert_not_called()
    mock_db["rds"].generate_db_auth_token.assert_not_called()


def test_handler_raises_when_email_is_blank(mock_db: Dict[str, MagicMock]) -> None:
    event = _build_event(email="")

    with pytest.raises(handler.PostConfirmationError, match="email"):
        handler.handler(event, context=None)

    mock_db["connect"].assert_not_called()


def test_handler_raises_when_sub_missing(mock_db: Dict[str, MagicMock]) -> None:
    """The Cognito sub is the join key for app_user; missing it is fatal."""
    event = _build_event(include_sub=False)
    # Force the userAttributes dict to truly omit "sub" — the helper
    # populates userName from the sub fallback otherwise.
    event["request"]["userAttributes"].pop("sub", None)

    with pytest.raises(handler.PostConfirmationError, match="sub"):
        handler.handler(event, context=None)


def test_handler_raises_when_db_host_env_missing(
    mock_db: Dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DB_HOST", raising=False)
    event = _build_event()

    with pytest.raises(handler.PostConfirmationError, match="DB_HOST"):
        handler.handler(event, context=None)


# ---------------------------------------------------------------------------
# Property-based tests.
# ---------------------------------------------------------------------------


# Restrict to printable Cognito-style identifiers and reasonable email
# shapes — Hypothesis would otherwise spend cycles on Unicode edge cases
# that Cognito itself rejects upstream.
_SUB_CHARS = st.text(
    alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z")),
    min_size=8,
    max_size=64,
)
_EMAIL_LOCAL = st.from_regex(r"[a-z][a-z0-9._-]{1,30}", fullmatch=True)
_EMAIL_DOMAIN = st.sampled_from(["example.org", "alleninstitute.org", "uw.edu", "test.example"])
_EMAIL = st.builds(lambda local, domain: f"{local}@{domain}", _EMAIL_LOCAL, _EMAIL_DOMAIN)


@given(sub=_SUB_CHARS, email=_EMAIL)
@settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
def test_property_handler_always_inserts_exactly_one_row_with_event_values(
    sub: str, email: str
) -> None:
    """For every well-formed event the Lambda issues exactly one INSERT
    carrying the event's sub and email, and returns the event unchanged.

    Validates: R19.3 (Post-Confirmation creates an `app_user` row).
    """
    cursor = MagicMock()
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    cursor.rowcount = 1
    conn = MagicMock()
    conn.cursor.return_value = cursor

    rds = MagicMock()
    rds.generate_db_auth_token.return_value = "tok"

    with patch.object(handler.pg8000.dbapi, "connect", return_value=conn), patch.object(
        handler.boto3, "client", return_value=rds
    ):
        event = _build_event(sub=sub, email=email)
        result = handler.handler(event, context=None)

    assert cursor.execute.call_count == 1
    sql, params = cursor.execute.call_args.args
    assert "INSERT INTO app_user" in sql
    assert "ON CONFLICT (cognito_sub) DO NOTHING" in sql
    assert params == (sub, email.strip())
    conn.commit.assert_called_once()
    assert result is event


@given(missing_field=st.sampled_from(["sub", "email"]))
@settings(max_examples=20, deadline=None)
def test_property_missing_required_field_always_raises(missing_field: str) -> None:
    """Removing either required attribute yields PostConfirmationError
    and never opens a DB connection.

    Validates: R19.3 (the row insert is gated on having both required
    attributes).
    """
    rds = MagicMock()
    rds.generate_db_auth_token.return_value = "tok"
    connect_mock = MagicMock()

    with patch.object(handler.pg8000.dbapi, "connect", connect_mock), patch.object(
        handler.boto3, "client", return_value=rds
    ):
        event = _build_event()
        event["request"]["userAttributes"].pop(missing_field, None)

        with pytest.raises(handler.PostConfirmationError):
            handler.handler(event, context=None)

    connect_mock.assert_not_called()
    rds.generate_db_auth_token.assert_not_called()

"""Unit tests for biodata_registry_shared.db.

We do not require a live psycopg; we inject a fake connection factory
so the test focuses on:

* IAM auth-token vs Secrets Manager credential resolution
* The four ``SET LOCAL app.current_*`` GUC issuance
* Commit-on-success / rollback-on-exception semantics
* Connection-close on all paths
"""
from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from biodata_registry_shared.auth_context import AuthContext
from biodata_registry_shared.db import (
    AuroraConnectionConfig,
    aurora_connection,
)


# ---------------------------------------------------------------------------
# Fake psycopg connection used by all tests
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _FakeCursor:
    parent: "_FakeConn"

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.parent.executed.append((sql, params))


@dataclasses.dataclass
class _FakeConn:
    """Records every executed statement; tracks commit / rollback / close."""

    connect_kwargs: dict[str, Any]
    executed: list[tuple[str, tuple[Any, ...] | None]] = dataclasses.field(
        default_factory=list
    )
    committed: bool = False
    rolled_back: bool = False
    closed: bool = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(parent=self)

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def _make_connection_factory() -> tuple[list[_FakeConn], Any]:
    """Build a connection factory that records the connections it produced."""
    produced: list[_FakeConn] = []

    def factory(**kwargs: Any) -> _FakeConn:
        conn = _FakeConn(connect_kwargs=dict(kwargs))
        produced.append(conn)
        return conn

    return produced, factory


def _make_auth(
    *,
    roles: set[str] = frozenset({"viewer"}),
    space_ids: set[str] = frozenset(),
    org_ids: set[str] = frozenset(),
) -> AuthContext:
    return AuthContext(
        user_id="11111111-1111-4111-8111-111111111111",
        cognito_sub="22222222-2222-4222-8222-222222222222",
        email="alice@example.org",
        org_ids=frozenset(org_ids),
        space_ids=frozenset(space_ids),
        roles=frozenset(roles),
    )


def _config() -> AuroraConnectionConfig:
    return AuroraConnectionConfig(
        host="aurora.example",
        database="biodata_registry",
        user="biodata_app",
        port=5432,
        region="us-west-2",
        sslmode="require",
        connect_timeout_s=5,
        statement_timeout_ms=5_000,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_iam_token_path_sets_all_four_gucs() -> None:
    auth = _make_auth(
        roles={"viewer", "data_administrator"},
        space_ids={"44444444-4444-4444-8444-444444444444"},
        org_ids={"33333333-3333-4333-8333-333333333333"},
    )
    produced, factory = _make_connection_factory()

    token_calls: list[dict[str, Any]] = []

    def stub_token_provider(**kwargs: Any) -> str:
        token_calls.append(kwargs)
        return "iam-token-abc"

    with aurora_connection(
        auth,
        config=_config(),
        connection_factory=factory,
        auth_token_provider=stub_token_provider,
    ) as conn:
        # No business statements executed by the helper itself
        # beyond BEGIN, four set_config calls, and statement_timeout.
        assert isinstance(conn, _FakeConn)
        # Inside the body we can observe the recorded statements.
        recorded = list(conn.executed)

    # Token resolution happened with the right inputs.
    assert token_calls == [
        {"host": "aurora.example", "port": 5432, "user": "biodata_app", "region": "us-west-2"}
    ]

    assert len(produced) == 1
    conn = produced[0]
    # Connection was opened with TLS + the IAM token as password.
    assert conn.connect_kwargs["host"] == "aurora.example"
    assert conn.connect_kwargs["password"] == "iam-token-abc"
    assert conn.connect_kwargs["sslmode"] == "require"
    assert conn.connect_kwargs["autocommit"] is False

    # First statement is BEGIN; then four set_config calls; then
    # statement_timeout.
    statements = [stmt for stmt, _ in recorded]
    assert statements[0] == "BEGIN"

    set_config_calls = [
        params for stmt, params in recorded if stmt.startswith("SELECT set_config")
    ]
    assert len(set_config_calls) == 4
    guc_names = sorted(p[0] for p in set_config_calls)
    assert guc_names == sorted(
        [
            "app.current_user_id",
            "app.current_org_ids",
            "app.current_space_ids",
            "app.current_user_role_set",
        ]
    )

    # Verify the actual values match AuthContext.to_guc_payload().
    by_name = {p[0]: p[1] for p in set_config_calls}
    assert by_name["app.current_user_id"] == auth.user_id
    assert by_name["app.current_org_ids"] == ",".join(sorted(auth.org_ids))
    assert by_name["app.current_space_ids"] == ",".join(sorted(auth.space_ids))
    assert by_name["app.current_user_role_set"] == ",".join(sorted(auth.roles))

    # statement_timeout was set
    assert any("statement_timeout = 5000" in stmt for stmt, _ in recorded)

    # Clean exit committed and closed.
    assert conn.committed is True
    assert conn.rolled_back is False
    assert conn.closed is True


def test_secrets_manager_path_does_not_call_iam_provider() -> None:
    auth = _make_auth()
    produced, factory = _make_connection_factory()

    iam_called = False

    def stub_token_provider(**kwargs: Any) -> str:
        nonlocal iam_called
        iam_called = True
        return "should-not-be-used"

    def stub_secret_provider(secret_arn: str, region: str) -> str:
        assert secret_arn == "arn:aws:secretsmanager:::test-secret"
        return "secret-password"

    with aurora_connection(
        auth,
        config=_config(),
        secret_arn="arn:aws:secretsmanager:::test-secret",
        connection_factory=factory,
        auth_token_provider=stub_token_provider,
        secret_password_provider=stub_secret_provider,
    ):
        pass

    assert iam_called is False
    assert produced[0].connect_kwargs["password"] == "secret-password"


def test_db_user_override_is_used() -> None:
    auth = _make_auth()
    produced, factory = _make_connection_factory()

    def stub_token_provider(**kwargs: Any) -> str:
        # Confirm the override flowed to the token provider too.
        assert kwargs["user"] == "alt_user"
        return "tok"

    with aurora_connection(
        auth,
        config=_config(),
        db_user="alt_user",
        connection_factory=factory,
        auth_token_provider=stub_token_provider,
    ):
        pass

    assert produced[0].connect_kwargs["user"] == "alt_user"


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_exception_in_body_triggers_rollback_and_close() -> None:
    auth = _make_auth()
    produced, factory = _make_connection_factory()

    class BodyError(RuntimeError):
        pass

    with pytest.raises(BodyError):
        with aurora_connection(
            auth,
            config=_config(),
            connection_factory=factory,
            auth_token_provider=lambda **_: "tok",
        ):
            raise BodyError("boom")

    conn = produced[0]
    assert conn.rolled_back is True
    assert conn.committed is False
    assert conn.closed is True


def test_invalid_auth_type_raises_at_call_time() -> None:
    with pytest.raises(TypeError):
        with aurora_connection(
            "not-an-AuthContext",  # type: ignore[arg-type]
            config=_config(),
            connection_factory=lambda **_: _FakeConn(connect_kwargs={}),
        ):
            pass


def test_statement_timeout_zero_skips_set_local() -> None:
    auth = _make_auth()
    produced, factory = _make_connection_factory()
    cfg = dataclasses.replace(_config(), statement_timeout_ms=0)

    with aurora_connection(
        auth,
        config=cfg,
        connection_factory=factory,
        auth_token_provider=lambda **_: "tok",
    ):
        pass

    conn = produced[0]
    assert all("statement_timeout" not in stmt for stmt, _ in conn.executed)


# ---------------------------------------------------------------------------
# from_env
# ---------------------------------------------------------------------------


def test_from_env_reads_required_vars(
    monkeypatch: pytest.MonkeyPatch,
    clear_db_env: None,  # noqa: ARG001 - fixture clears env
) -> None:
    monkeypatch.setenv("DB_HOST", "envhost")
    monkeypatch.setenv("DB_NAME", "envdb")
    monkeypatch.setenv("DB_USER", "envuser")
    cfg = AuroraConnectionConfig.from_env()
    assert cfg.host == "envhost"
    assert cfg.database == "envdb"
    assert cfg.user == "envuser"
    # Defaults
    assert cfg.port == 5432
    assert cfg.sslmode == "require"
    assert cfg.connect_timeout_s == 10


def test_from_env_raises_on_missing_required(
    monkeypatch: pytest.MonkeyPatch,
    clear_db_env: None,  # noqa: ARG001
) -> None:
    monkeypatch.setenv("DB_HOST", "envhost")
    # DB_NAME and DB_USER intentionally missing
    with pytest.raises(RuntimeError, match="DB_NAME"):
        AuroraConnectionConfig.from_env()

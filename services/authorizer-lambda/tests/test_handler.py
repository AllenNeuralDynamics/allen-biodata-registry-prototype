"""Unit tests for the API Gateway Authorizer Lambda.

The tests exercise the seven behaviors required by Task 15.1:

1. Valid JWT + valid app_user row → returns Allow policy with correct
   context.
2. Invalid JWT signature → raises Unauthorized.
3. Expired JWT → raises Unauthorized.
4. Missing app_user row → raises Unauthorized.
5. Multiple roles (org_admin + space_admin from different rows) → roles
   aggregated correctly.
6. Active sharing_grant for the user → grantee_space_id added to
   space_ids.
7. Expired sharing_grant → NOT added to space_ids.

Plus operational paths: missing Authorization header, wrong audience,
access tokens (token_use != "id"), bearer-prefix stripping, JWKS cache
behaviour.

JWT signing keys are minted with the ``cryptography`` library so the
tokens go through the real PyJWT decode path — only the JWKS lookup is
stubbed via ``unittest.mock``. The DB cursor is stubbed too, so no
network or filesystem dependency.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from unittest.mock import MagicMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from hypothesis import HealthCheck, given, settings, strategies as st

import handler


# ---------------------------------------------------------------------------
# Constants reused across tests.
# ---------------------------------------------------------------------------

USER_POOL_ID = "us-west-2_TESTPOOL"
APP_CLIENT_ID = "0123456789abcdefghijklmnop"
REGION = "us-west-2"
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
COGNITO_SUB = "11111111-2222-3333-4444-555555555555"
APP_USER_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
EMAIL = "researcher@alleninstitute.org"
METHOD_ARN = (
    "arn:aws:execute-api:us-west-2:123456789012:abc123def4/dev/GET/assets"
)


# ---------------------------------------------------------------------------
# RSA key fixtures — generated once per session because RSA keygen is
# expensive (~100ms).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def rsa_keypair() -> Tuple[Any, Any]:
    """Return a freshly-generated 2048-bit RSA keypair (private, public).

    Cognito signs ID tokens with RS256, so the test token must be
    signed with a private RSA key the matching public key of which is
    served from the (mocked) JWKS endpoint.
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    return private_key, public_key


@pytest.fixture(scope="session")
def signing_key_pem(rsa_keypair: Tuple[Any, Any]) -> str:
    """Return the private key as a PEM string suitable for jwt.encode."""
    private_key, _ = rsa_keypair
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


# ---------------------------------------------------------------------------
# Environment + module-state fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def authorizer_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject the env vars Terraform provides to the Lambda."""
    monkeypatch.setenv("COGNITO_USER_POOL_ID", USER_POOL_ID)
    monkeypatch.setenv("COGNITO_APP_CLIENT_ID", APP_CLIENT_ID)
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("DB_HOST", "biodata-registry-dev-aurora.cluster-abc.us-west-2.rds.amazonaws.com")
    monkeypatch.setenv("DB_PORT", "5432")
    monkeypatch.setenv("DB_NAME", "biodata_registry")
    monkeypatch.setenv("DB_USER", "authorizer_lambda")


@pytest.fixture(autouse=True)
def reset_jwks_cache() -> None:
    """Wipe the module-level JWKS cache before every test.

    Without this, a stub installed for one test would be reused by the
    next — the cache is keyed on the JWKS URL, which is constant across
    tests.
    """
    handler._jwks_cache.clear()
    yield
    handler._jwks_cache.clear()


# ---------------------------------------------------------------------------
# Helpers — token + event builders, DB-cursor stub.
# ---------------------------------------------------------------------------


def _make_token(
    *,
    signing_key_pem: str,
    sub: str = COGNITO_SUB,
    audience: str = APP_CLIENT_ID,
    issuer: str = ISSUER,
    token_use: str = "id",
    email: str = EMAIL,
    expires_in_seconds: int = 3600,
    issued_at_offset: int = 0,
    extra_claims: Optional[Mapping[str, Any]] = None,
    sign_with_pem: Optional[str] = None,
) -> str:
    """Mint a Cognito-shaped ID token signed with the test private key.

    ``sign_with_pem`` lets a test sign with a *different* private key
    while keeping the JWKS pointing at the original public key — used
    to provoke ``InvalidSignatureError``.
    """
    now = int(time.time()) + issued_at_offset
    payload: Dict[str, Any] = {
        "sub": sub,
        "aud": audience,
        "iss": issuer,
        "iat": now,
        "exp": now + expires_in_seconds,
        "token_use": token_use,
        "email": email,
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, sign_with_pem or signing_key_pem, algorithm="RS256")


def _build_event(
    *,
    token: str,
    method_arn: str = METHOD_ARN,
    bearer_prefix: bool = True,
    header_case: str = "Authorization",
) -> Mapping[str, Any]:
    """Construct an API Gateway REQUEST authorizer event."""
    auth_value = f"Bearer {token}" if bearer_prefix else token
    return {
        "type": "REQUEST",
        "methodArn": method_arn,
        "headers": {header_case: auth_value},
    }


class StubCursor:
    """Stand-in for a psycopg cursor.

    Returns canned rows in the same order the handler issues queries:
    ``app_user`` lookup, then ``user_org_role``, then
    ``user_space_role``, then ``space`` (when org-level roles exist),
    then ``sharing_grant`` join.

    The handler may skip the ``space`` query entirely when the user has
    no org-level roles, so this stub matches each query against a
    fragment of its SQL text and dispatches accordingly. That's
    deliberately loose — we don't care about the exact whitespace, we
    care about the table name.
    """

    def __init__(
        self,
        *,
        app_user_row: Optional[Tuple[str, str]],
        org_roles: Sequence[Tuple[str, str]] = (),
        space_roles: Sequence[Tuple[str, str]] = (),
        org_spaces: Sequence[str] = (),
        sharing_spaces: Sequence[str] = (),
    ) -> None:
        self._app_user_row = app_user_row
        self._org_roles = list(org_roles)
        self._space_roles = list(space_roles)
        self._org_spaces = list(org_spaces)
        self._sharing_spaces = list(sharing_spaces)
        self._next_result: List[Any] = []
        self._next_one: Optional[Any] = None
        self.executed_sql: List[Tuple[str, Tuple[Any, ...]]] = []

    def __enter__(self) -> "StubCursor":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def execute(self, sql: str, params: Tuple[Any, ...] = ()) -> None:
        self.executed_sql.append((sql, params))
        lowered = sql.lower()
        if "from app_user" in lowered:
            self._next_one = self._app_user_row
            self._next_result = []
        elif "from user_org_role" in lowered:
            self._next_result = list(self._org_roles)
        elif "from user_space_role" in lowered:
            self._next_result = list(self._space_roles)
        elif "from space " in lowered or lowered.endswith("from space"):
            self._next_result = [(s,) for s in self._org_spaces]
        elif "from sharing_grant" in lowered:
            self._next_result = [(s,) for s in self._sharing_spaces]
        else:  # pragma: no cover - tests should cover every branch
            raise AssertionError(f"unexpected SQL: {sql!r}")

    def fetchone(self) -> Optional[Tuple[Any, ...]]:
        return self._next_one

    def fetchall(self) -> List[Tuple[Any, ...]]:
        return list(self._next_result)


def _install_db_stub(
    monkeypatch: pytest.MonkeyPatch,
    cursor: StubCursor,
) -> MagicMock:
    """Stub out psycopg.connect + boto3.client('rds').

    Returns the connection mock so tests can assert on close() etc.
    """
    conn = MagicMock(name="aurora_connection")
    conn.cursor.return_value = cursor

    monkeypatch.setattr(handler.psycopg, "connect", MagicMock(return_value=conn))

    rds_client = MagicMock(name="rds_client")
    rds_client.generate_db_auth_token.return_value = "fake-iam-token"
    monkeypatch.setattr(handler.boto3, "client", MagicMock(return_value=rds_client))

    return conn


def _install_jwks_stub(
    monkeypatch: pytest.MonkeyPatch,
    public_key: Any,
) -> MagicMock:
    """Stub PyJWKClient so it returns the test RSA public key.

    Returns the mock so tests can assert on it (e.g. cache reuse
    counts the constructor calls).
    """
    signing_key = MagicMock(name="signing_key")
    signing_key.key = public_key

    jwk_client = MagicMock(name="PyJWKClient")
    jwk_client.get_signing_key_from_jwt.return_value = signing_key

    constructor = MagicMock(return_value=jwk_client)
    monkeypatch.setattr(handler, "PyJWKClient", constructor)
    return constructor


# ---------------------------------------------------------------------------
# 1) Valid JWT + valid app_user row → Allow policy.
# ---------------------------------------------------------------------------


def test_valid_jwt_returns_allow_policy(
    monkeypatch: pytest.MonkeyPatch,
    rsa_keypair: Tuple[Any, Any],
    signing_key_pem: str,
) -> None:
    _, public_key = rsa_keypair
    _install_jwks_stub(monkeypatch, public_key)

    org_id = str(uuid.uuid4())
    space_id = str(uuid.uuid4())
    org_space_id = str(uuid.uuid4())
    cursor = StubCursor(
        app_user_row=(APP_USER_ID, EMAIL),
        org_roles=[(org_id, "viewer")],
        space_roles=[(space_id, "data_administrator")],
        org_spaces=[org_space_id],
        sharing_spaces=[],
    )
    _install_db_stub(monkeypatch, cursor)

    token = _make_token(signing_key_pem=signing_key_pem)
    event = _build_event(token=token)

    result = handler.handler(event, context=None)

    # Policy shape.
    assert result["principalId"] == COGNITO_SUB
    assert result["policyDocument"]["Version"] == "2012-10-17"
    statement = result["policyDocument"]["Statement"][0]
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == "execute-api:Invoke"
    assert statement["Resource"] == METHOD_ARN

    # Context payload: every list field is comma-joined.
    ctx = result["context"]
    assert ctx["user_id"] == APP_USER_ID
    assert ctx["cognito_sub"] == COGNITO_SUB
    assert ctx["email"] == EMAIL
    assert ctx["roles"] == "data_administrator,viewer"  # sorted
    assert ctx["org_ids"] == org_id
    # space_ids includes the direct space role + every space in the
    # user's orgs (org-level role inheritance).
    space_ids_set = set(ctx["space_ids"].split(","))
    assert space_ids_set == {space_id, org_space_id}


# ---------------------------------------------------------------------------
# 2) Invalid JWT signature → Unauthorized.
# ---------------------------------------------------------------------------


def test_invalid_jwt_signature_raises_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
    rsa_keypair: Tuple[Any, Any],
    signing_key_pem: str,
) -> None:
    """Sign the token with a *different* RSA key than the JWKS publishes."""
    _, public_key = rsa_keypair
    _install_jwks_stub(monkeypatch, public_key)
    # Cursor / DB never get hit because signature verification fails first.
    _install_db_stub(monkeypatch, StubCursor(app_user_row=None))

    bogus_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    bogus_pem = bogus_private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")

    token = _make_token(signing_key_pem=signing_key_pem, sign_with_pem=bogus_pem)
    event = _build_event(token=token)

    with pytest.raises(handler.Unauthorized) as exc_info:
        handler.handler(event, context=None)
    assert str(exc_info.value) == "Unauthorized"


# ---------------------------------------------------------------------------
# 3) Expired JWT → Unauthorized.
# ---------------------------------------------------------------------------


def test_expired_jwt_raises_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
    rsa_keypair: Tuple[Any, Any],
    signing_key_pem: str,
) -> None:
    _, public_key = rsa_keypair
    _install_jwks_stub(monkeypatch, public_key)
    _install_db_stub(monkeypatch, StubCursor(app_user_row=None))

    # iat 2 hours ago, exp 1 hour ago.
    token = _make_token(
        signing_key_pem=signing_key_pem,
        issued_at_offset=-7200,
        expires_in_seconds=3600,
    )
    event = _build_event(token=token)

    with pytest.raises(handler.Unauthorized) as exc_info:
        handler.handler(event, context=None)
    assert str(exc_info.value) == "Unauthorized"


# ---------------------------------------------------------------------------
# 4) Missing app_user row → Unauthorized.
# ---------------------------------------------------------------------------


def test_missing_app_user_row_raises_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
    rsa_keypair: Tuple[Any, Any],
    signing_key_pem: str,
) -> None:
    _, public_key = rsa_keypair
    _install_jwks_stub(monkeypatch, public_key)
    cursor = StubCursor(app_user_row=None)  # no app_user row
    _install_db_stub(monkeypatch, cursor)

    token = _make_token(signing_key_pem=signing_key_pem)
    event = _build_event(token=token)

    with pytest.raises(handler.Unauthorized) as exc_info:
        handler.handler(event, context=None)
    assert str(exc_info.value) == "Unauthorized"


# ---------------------------------------------------------------------------
# 5) Multiple roles aggregated correctly.
# ---------------------------------------------------------------------------


def test_multiple_roles_aggregated_correctly(
    monkeypatch: pytest.MonkeyPatch,
    rsa_keypair: Tuple[Any, Any],
    signing_key_pem: str,
) -> None:
    _, public_key = rsa_keypair
    _install_jwks_stub(monkeypatch, public_key)

    org_a = str(uuid.uuid4())
    org_b = str(uuid.uuid4())
    space_a = str(uuid.uuid4())
    space_b = str(uuid.uuid4())

    cursor = StubCursor(
        app_user_row=(APP_USER_ID, EMAIL),
        # User has org_admin on org_a AND viewer on org_b.
        org_roles=[(org_a, "org_admin"), (org_b, "viewer")],
        # Plus space_admin on space_a (a space inside some other org)
        # and data_administrator on space_b.
        space_roles=[(space_a, "space_admin"), (space_b, "data_administrator")],
        org_spaces=[],
        sharing_spaces=[],
    )
    _install_db_stub(monkeypatch, cursor)

    token = _make_token(signing_key_pem=signing_key_pem)
    event = _build_event(token=token)

    result = handler.handler(event, context=None)
    ctx = result["context"]

    roles = ctx["roles"].split(",")
    assert sorted(roles) == ["data_administrator", "org_admin", "space_admin", "viewer"]
    assert sorted(ctx["org_ids"].split(",")) == sorted([org_a, org_b])
    assert sorted(ctx["space_ids"].split(",")) == sorted([space_a, space_b])


# ---------------------------------------------------------------------------
# 6) Active sharing_grant adds grantee_space_id to space_ids.
# ---------------------------------------------------------------------------


def test_active_sharing_grant_adds_space_to_context(
    monkeypatch: pytest.MonkeyPatch,
    rsa_keypair: Tuple[Any, Any],
    signing_key_pem: str,
) -> None:
    _, public_key = rsa_keypair
    _install_jwks_stub(monkeypatch, public_key)

    direct_space = str(uuid.uuid4())
    shared_space = str(uuid.uuid4())

    cursor = StubCursor(
        app_user_row=(APP_USER_ID, EMAIL),
        org_roles=[],
        space_roles=[(direct_space, "viewer")],
        org_spaces=[],
        # The sharing_grant query (after the WHERE filter for
        # not-yet-expired) returns this space — the active grant.
        sharing_spaces=[shared_space],
    )
    _install_db_stub(monkeypatch, cursor)

    token = _make_token(signing_key_pem=signing_key_pem)
    event = _build_event(token=token)

    result = handler.handler(event, context=None)
    space_ids = set(result["context"]["space_ids"].split(","))
    assert space_ids == {direct_space, shared_space}


# ---------------------------------------------------------------------------
# 7) Expired sharing_grant is NOT added to space_ids.
# ---------------------------------------------------------------------------


def test_expired_sharing_grant_excluded_from_context(
    monkeypatch: pytest.MonkeyPatch,
    rsa_keypair: Tuple[Any, Any],
    signing_key_pem: str,
) -> None:
    """The expiration filter (`expires_at IS NULL OR expires_at > now()`)
    is evaluated server-side in Aurora. We model the expected outcome
    by simply not returning the expired space from the cursor stub —
    matching what the actual SQL would do.

    Additionally we assert the SQL the handler issued contains the
    expiration predicate, so the test would fail if the handler were
    to drop the filter.
    """
    _, public_key = rsa_keypair
    _install_jwks_stub(monkeypatch, public_key)

    direct_space = str(uuid.uuid4())
    cursor = StubCursor(
        app_user_row=(APP_USER_ID, EMAIL),
        org_roles=[],
        space_roles=[(direct_space, "viewer")],
        org_spaces=[],
        # Empty: the only sharing_grant for this user has already
        # expired and the SQL's WHERE clause filters it out.
        sharing_spaces=[],
    )
    _install_db_stub(monkeypatch, cursor)

    token = _make_token(signing_key_pem=signing_key_pem)
    event = _build_event(token=token)

    result = handler.handler(event, context=None)
    space_ids = set(result["context"]["space_ids"].split(","))
    assert space_ids == {direct_space}

    sharing_grant_sql = next(
        sql for (sql, _params) in cursor.executed_sql
        if "from sharing_grant" in sql.lower()
    )
    # The handler must filter on expires_at.
    assert "expires_at" in sharing_grant_sql
    assert "now()" in sharing_grant_sql.lower()


# ---------------------------------------------------------------------------
# Operational paths.
# ---------------------------------------------------------------------------


def test_missing_authorization_header_raises_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
    rsa_keypair: Tuple[Any, Any],
) -> None:
    _, public_key = rsa_keypair
    _install_jwks_stub(monkeypatch, public_key)
    _install_db_stub(monkeypatch, StubCursor(app_user_row=None))

    event = {"type": "REQUEST", "methodArn": METHOD_ARN, "headers": {}}

    with pytest.raises(handler.Unauthorized):
        handler.handler(event, context=None)


def test_lowercase_authorization_header_accepted(
    monkeypatch: pytest.MonkeyPatch,
    rsa_keypair: Tuple[Any, Any],
    signing_key_pem: str,
) -> None:
    _, public_key = rsa_keypair
    _install_jwks_stub(monkeypatch, public_key)
    cursor = StubCursor(
        app_user_row=(APP_USER_ID, EMAIL),
        org_roles=[],
        space_roles=[],
        org_spaces=[],
        sharing_spaces=[],
    )
    _install_db_stub(monkeypatch, cursor)

    token = _make_token(signing_key_pem=signing_key_pem)
    event = _build_event(token=token, header_case="authorization")

    result = handler.handler(event, context=None)
    assert result["principalId"] == COGNITO_SUB


def test_bearer_prefix_stripped(
    monkeypatch: pytest.MonkeyPatch,
    rsa_keypair: Tuple[Any, Any],
    signing_key_pem: str,
) -> None:
    """Header value `Bearer <token>` and bare `<token>` must both work."""
    _, public_key = rsa_keypair
    _install_jwks_stub(monkeypatch, public_key)
    cursor = StubCursor(
        app_user_row=(APP_USER_ID, EMAIL),
        org_roles=[],
        space_roles=[],
        org_spaces=[],
        sharing_spaces=[],
    )
    _install_db_stub(monkeypatch, cursor)

    token = _make_token(signing_key_pem=signing_key_pem)
    # Bare token — no Bearer prefix.
    event = _build_event(token=token, bearer_prefix=False)

    result = handler.handler(event, context=None)
    assert result["principalId"] == COGNITO_SUB


def test_wrong_audience_raises_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
    rsa_keypair: Tuple[Any, Any],
    signing_key_pem: str,
) -> None:
    _, public_key = rsa_keypair
    _install_jwks_stub(monkeypatch, public_key)
    _install_db_stub(monkeypatch, StubCursor(app_user_row=None))

    token = _make_token(signing_key_pem=signing_key_pem, audience="some-other-client")
    event = _build_event(token=token)

    with pytest.raises(handler.Unauthorized):
        handler.handler(event, context=None)


def test_wrong_issuer_raises_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
    rsa_keypair: Tuple[Any, Any],
    signing_key_pem: str,
) -> None:
    _, public_key = rsa_keypair
    _install_jwks_stub(monkeypatch, public_key)
    _install_db_stub(monkeypatch, StubCursor(app_user_row=None))

    token = _make_token(
        signing_key_pem=signing_key_pem,
        issuer="https://cognito-idp.us-west-2.amazonaws.com/us-west-2_OTHERPOOL",
    )
    event = _build_event(token=token)

    with pytest.raises(handler.Unauthorized):
        handler.handler(event, context=None)


def test_access_token_rejected(
    monkeypatch: pytest.MonkeyPatch,
    rsa_keypair: Tuple[Any, Any],
    signing_key_pem: str,
) -> None:
    """token_use must be 'id'; access tokens lack the email claim and
    are rejected at the boundary."""
    _, public_key = rsa_keypair
    _install_jwks_stub(monkeypatch, public_key)
    _install_db_stub(monkeypatch, StubCursor(app_user_row=None))

    token = _make_token(signing_key_pem=signing_key_pem, token_use="access")
    event = _build_event(token=token)

    with pytest.raises(handler.Unauthorized):
        handler.handler(event, context=None)


def test_jwks_cache_is_reused_across_invocations(
    monkeypatch: pytest.MonkeyPatch,
    rsa_keypair: Tuple[Any, Any],
    signing_key_pem: str,
) -> None:
    """Two back-to-back invocations should construct PyJWKClient at most
    once because the module-level cache lives across calls."""
    _, public_key = rsa_keypair
    constructor = _install_jwks_stub(monkeypatch, public_key)

    cursor = StubCursor(
        app_user_row=(APP_USER_ID, EMAIL),
        org_roles=[],
        space_roles=[],
        org_spaces=[],
        sharing_spaces=[],
    )
    _install_db_stub(monkeypatch, cursor)

    token = _make_token(signing_key_pem=signing_key_pem)
    event = _build_event(token=token)

    handler.handler(event, context=None)
    handler.handler(event, context=None)

    # PyJWKClient was instantiated exactly once.
    assert constructor.call_count == 1


def test_method_arn_used_as_resource(
    monkeypatch: pytest.MonkeyPatch,
    rsa_keypair: Tuple[Any, Any],
    signing_key_pem: str,
) -> None:
    """The Allow policy's Resource must match the method ARN exactly so
    the policy authorizes only the requested method (not the entire
    API)."""
    _, public_key = rsa_keypair
    _install_jwks_stub(monkeypatch, public_key)
    cursor = StubCursor(
        app_user_row=(APP_USER_ID, EMAIL),
        org_roles=[],
        space_roles=[],
        org_spaces=[],
        sharing_spaces=[],
    )
    _install_db_stub(monkeypatch, cursor)

    method_arn = (
        "arn:aws:execute-api:us-west-2:123456789012:abc/dev/POST/governance/spaces"
    )
    token = _make_token(signing_key_pem=signing_key_pem)
    event = _build_event(token=token, method_arn=method_arn)

    result = handler.handler(event, context=None)
    assert result["policyDocument"]["Statement"][0]["Resource"] == method_arn


def test_required_env_var_missing_raises_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
    rsa_keypair: Tuple[Any, Any],
    signing_key_pem: str,
) -> None:
    _, public_key = rsa_keypair
    _install_jwks_stub(monkeypatch, public_key)
    _install_db_stub(monkeypatch, StubCursor(app_user_row=None))

    monkeypatch.delenv("COGNITO_USER_POOL_ID", raising=False)
    token = _make_token(signing_key_pem=signing_key_pem)
    event = _build_event(token=token)

    with pytest.raises(handler.Unauthorized):
        handler.handler(event, context=None)


def test_no_org_roles_skips_inheritance_query(
    monkeypatch: pytest.MonkeyPatch,
    rsa_keypair: Tuple[Any, Any],
    signing_key_pem: str,
) -> None:
    """When the user has no org-level roles, the inheritance query is
    skipped (no point in `SELECT id FROM space WHERE org_id = ANY('{}')`).
    """
    _, public_key = rsa_keypair
    _install_jwks_stub(monkeypatch, public_key)

    space = str(uuid.uuid4())
    cursor = StubCursor(
        app_user_row=(APP_USER_ID, EMAIL),
        org_roles=[],
        space_roles=[(space, "viewer")],
        org_spaces=[],
        sharing_spaces=[],
    )
    _install_db_stub(monkeypatch, cursor)

    token = _make_token(signing_key_pem=signing_key_pem)
    event = _build_event(token=token)

    handler.handler(event, context=None)

    # No SELECT against `space` was issued.
    assert not any(
        ("from space " in sql.lower() or sql.lower().endswith("from space"))
        for (sql, _params) in cursor.executed_sql
    )


# ---------------------------------------------------------------------------
# Property-based tests — the policy shape always satisfies the API
# Gateway contract for any well-formed Cognito sub + Aurora row.
# ---------------------------------------------------------------------------


_SUB_STRATEGY = st.uuids().map(str)
_ROLE_STRATEGY = st.sampled_from(
    ["org_admin", "space_admin", "data_administrator", "viewer"]
)


@given(
    sub=_SUB_STRATEGY,
    extra_roles=st.lists(_ROLE_STRATEGY, min_size=0, max_size=4, unique=True),
)
@settings(
    max_examples=30,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
def test_property_policy_shape_is_well_formed(
    sub: str,
    extra_roles: List[str],
    monkeypatch: pytest.MonkeyPatch,
    rsa_keypair: Tuple[Any, Any],
    signing_key_pem: str,
) -> None:
    """For every well-formed Cognito sub and role set, the returned
    policy is JSON-serializable, has the correct top-level keys, and
    the context fields round-trip through ``parse_auth_context``-style
    string splitting.

    Validates: R19.5 (auth context populated for downstream Lambdas).
    """
    _, public_key = rsa_keypair
    _install_jwks_stub(monkeypatch, public_key)

    user_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())
    cursor = StubCursor(
        app_user_row=(user_id, EMAIL),
        org_roles=[(org_id, role) for role in extra_roles] if extra_roles else [],
        space_roles=[],
        org_spaces=[],
        sharing_spaces=[],
    )
    _install_db_stub(monkeypatch, cursor)

    token = _make_token(signing_key_pem=signing_key_pem, sub=sub)
    event = _build_event(token=token)

    result = handler.handler(event, context=None)

    # Top-level shape.
    assert set(result.keys()) == {"principalId", "policyDocument", "context"}
    assert result["principalId"] == sub
    assert result["policyDocument"]["Version"] == "2012-10-17"
    statement = result["policyDocument"]["Statement"][0]
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == "execute-api:Invoke"

    # Context — every value is a string (API Gateway requirement).
    ctx = result["context"]
    for key, value in ctx.items():
        assert isinstance(value, str), f"context.{key} is not a string"

    # JSON-serializable end-to-end (no datetime / UUID leakage).
    json.dumps(result)

    # Roles round-trip cleanly.
    parsed_roles = (
        set(ctx["roles"].split(",")) - {""} if ctx["roles"] else set()
    )
    assert parsed_roles == set(extra_roles)


# ---------------------------------------------------------------------------
# Task 15.2 acceptance — explicit "context fields populated for downstream
# Lambdas" check + JSON round-trip regression.
#
# These two tests pin the 15.2 acceptance criteria as crisp, dedicated
# assertions rather than scattering the checks across the
# valid-JWT-returns-Allow test and the property test. They protect
# against silent regressions where a refactor drops a context key the
# Indexing/Validation/Search Lambdas rely on, or returns a non-string
# type that API Gateway would silently coerce or drop.
# ---------------------------------------------------------------------------


# Every key that downstream Lambdas (parsed by
# biodata_registry_shared.parse_auth_context) consume to seed RLS GUCs
# and apply Layer 1 / Layer 3 enforcement.
_REQUIRED_CONTEXT_KEYS = (
    "user_id",
    "cognito_sub",
    "email",
    "roles",
    "org_ids",
    "space_ids",
)


def test_context_fields_populated_for_downstream_lambdas(
    monkeypatch: pytest.MonkeyPatch,
    rsa_keypair: Tuple[Any, Any],
    signing_key_pem: str,
) -> None:
    """Every context key the downstream Lambdas consume must be present,
    non-empty, and string-typed in the Allow policy.

    API Gateway flattens the ``context`` object to string-only values
    on the wire — ``None`` and lists/dicts get silently dropped or
    serialized in ways that break ``parse_auth_context``. This test
    pins the contract.

    Validates: R19.5 (context fields populated for downstream Lambdas).
    """
    _, public_key = rsa_keypair
    _install_jwks_stub(monkeypatch, public_key)

    org_id = str(uuid.uuid4())
    space_id = str(uuid.uuid4())
    cursor = StubCursor(
        app_user_row=(APP_USER_ID, EMAIL),
        org_roles=[(org_id, "org_admin")],
        space_roles=[(space_id, "data_administrator")],
        org_spaces=[],
        sharing_spaces=[],
    )
    _install_db_stub(monkeypatch, cursor)

    token = _make_token(signing_key_pem=signing_key_pem)
    event = _build_event(token=token)

    result = handler.handler(event, context=None)
    ctx = result["context"]

    # Exactly the documented key set — no extras (which would bloat the
    # context payload) and no omissions (which would break downstream).
    assert set(ctx.keys()) == set(_REQUIRED_CONTEXT_KEYS)

    for key in _REQUIRED_CONTEXT_KEYS:
        value = ctx[key]
        assert isinstance(value, str), (
            f"context.{key} must be a string for API Gateway, got {type(value).__name__}"
        )
        assert value != "", (
            f"context.{key} is empty; downstream RLS GUC seeding would be malformed"
        )

    # Spot-check the field-level invariants that the comma-join contract
    # relies on so a future refactor can't silently switch to JSON
    # arrays without breaking parse_auth_context.
    assert "," not in ctx["user_id"]
    assert "," not in ctx["cognito_sub"]
    assert "," not in ctx["email"]
    # Roles, org_ids, space_ids are comma-joined — at least one entry
    # given the cursor stub's seed data.
    assert "org_admin" in ctx["roles"].split(",")
    assert "data_administrator" in ctx["roles"].split(",")
    assert org_id in ctx["org_ids"].split(",")
    assert space_id in ctx["space_ids"].split(",")


def test_allow_policy_is_json_serializable_and_round_trips(
    monkeypatch: pytest.MonkeyPatch,
    rsa_keypair: Tuple[Any, Any],
    signing_key_pem: str,
) -> None:
    """The Allow policy must be JSON-serializable end-to-end and survive
    a ``json.dumps`` -> ``json.loads`` round-trip without loss.

    API Gateway serializes the authorizer's return value to JSON before
    handing it back to the Lambda invocation pipeline. A ``UUID`` or
    ``datetime`` leaking into the policy would crash the request with a
    500 at the gateway boundary — invisible to unit tests that compare
    Python dicts directly. This regression test catches that class of
    bug at the seam.

    Mirrors the spirit of design.md §Correctness Properties.Property 14
    (error response shapes are well-formed JSON), applied to the
    authorizer's success path.

    Validates: R19.5.
    """
    _, public_key = rsa_keypair
    _install_jwks_stub(monkeypatch, public_key)

    org_id = str(uuid.uuid4())
    space_id = str(uuid.uuid4())
    sharing_space = str(uuid.uuid4())
    cursor = StubCursor(
        app_user_row=(APP_USER_ID, EMAIL),
        org_roles=[(org_id, "viewer")],
        space_roles=[(space_id, "space_admin")],
        org_spaces=[],
        sharing_spaces=[sharing_space],
    )
    _install_db_stub(monkeypatch, cursor)

    token = _make_token(signing_key_pem=signing_key_pem)
    event = _build_event(token=token)

    result = handler.handler(event, context=None)

    serialized = json.dumps(result)
    round_tripped = json.loads(serialized)

    # Equality holds — no UUID/datetime/Decimal silently coerced to a
    # different representation.
    assert round_tripped == result

    # The policyDocument is the exact shape API Gateway expects.
    policy_doc = round_tripped["policyDocument"]
    assert policy_doc["Version"] == "2012-10-17"
    statement = policy_doc["Statement"][0]
    assert statement == {
        "Action": "execute-api:Invoke",
        "Effect": "Allow",
        "Resource": METHOD_ARN,
    }

    # Context survives the round-trip with all 6 required keys intact.
    assert set(round_tripped["context"].keys()) == set(_REQUIRED_CONTEXT_KEYS)

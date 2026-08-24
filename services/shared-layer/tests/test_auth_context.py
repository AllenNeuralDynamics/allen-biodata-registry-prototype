"""Unit + property tests for biodata_registry_shared.auth_context."""
from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from biodata_registry_shared.auth_context import (
    AuthContext,
    AuthContextError,
    parse_auth_context,
)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_parse_authorizer_dict_directly(valid_authorizer_context: dict[str, str]) -> None:
    ctx = parse_auth_context(valid_authorizer_context)
    assert ctx.user_id == "11111111-1111-4111-8111-111111111111"
    assert ctx.cognito_sub == "22222222-2222-4222-8222-222222222222"
    assert ctx.email == "alice@alleninstitute.org"
    assert ctx.roles == frozenset({"viewer", "data_administrator"})
    assert ctx.org_ids == frozenset({"33333333-3333-4333-8333-333333333333"})
    assert ctx.space_ids == frozenset(
        {
            "44444444-4444-4444-8444-444444444444",
            "55555555-5555-4555-8555-555555555555",
        }
    )


def test_parse_full_lambda_proxy_event(valid_authorizer_context: dict[str, str]) -> None:
    """The parser also handles the full API Gateway proxy event shape."""
    event = {
        "httpMethod": "GET",
        "path": "/assets",
        "requestContext": {
            "authorizer": valid_authorizer_context,
        },
    }
    ctx = parse_auth_context(event)
    assert ctx.user_id == valid_authorizer_context["user_id"]


def test_parse_http_api_v2_jwt_claims(valid_authorizer_context: dict[str, str]) -> None:
    """HTTP API v2 nests claims under requestContext.authorizer.jwt.claims."""
    event = {
        "requestContext": {
            "authorizer": {
                "jwt": {
                    "claims": valid_authorizer_context,
                },
                # Top-level authorizer fields override claims; here we
                # leave them blank to confirm the claims path is read.
            }
        }
    }
    ctx = parse_auth_context(event)
    assert ctx.user_id == valid_authorizer_context["user_id"]


def test_authorizer_top_level_overrides_jwt_claims(
    valid_authorizer_context: dict[str, str],
) -> None:
    other_user = "99999999-9999-4999-8999-999999999999"
    event = {
        "requestContext": {
            "authorizer": {
                "jwt": {"claims": valid_authorizer_context},
                # Note: roles/email also have to be present at the
                # top level for the parser to succeed when the merged
                # dict is fed back through validation.
                "user_id": other_user,
                "cognito_sub": valid_authorizer_context["cognito_sub"],
                "email": valid_authorizer_context["email"],
                "roles": valid_authorizer_context["roles"],
                "org_ids": valid_authorizer_context["org_ids"],
                "space_ids": valid_authorizer_context["space_ids"],
            }
        }
    }
    ctx = parse_auth_context(event)
    assert ctx.user_id == other_user


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "missing_field",
    ["user_id", "cognito_sub", "email", "roles"],
)
def test_missing_required_field_raises(
    valid_authorizer_context: dict[str, str],
    missing_field: str,
) -> None:
    auth = dict(valid_authorizer_context)
    auth.pop(missing_field)
    with pytest.raises(AuthContextError) as exc_info:
        parse_auth_context(auth)
    # The error should name the missing field so operators can fix
    # their authorizer.
    assert missing_field in str(exc_info.value) or "roles" in str(exc_info.value)


def test_empty_org_ids_is_allowed(valid_authorizer_context: dict[str, str]) -> None:
    """Users with no org-level role still need a parseable context."""
    auth = dict(valid_authorizer_context)
    auth["org_ids"] = ""
    ctx = parse_auth_context(auth)
    assert ctx.org_ids == frozenset()


def test_empty_space_ids_is_allowed(valid_authorizer_context: dict[str, str]) -> None:
    auth = dict(valid_authorizer_context)
    auth["space_ids"] = ""
    ctx = parse_auth_context(auth)
    assert ctx.space_ids == frozenset()


def test_unknown_role_is_rejected(valid_authorizer_context: dict[str, str]) -> None:
    auth = dict(valid_authorizer_context)
    auth["roles"] = "viewer,not_a_role"
    with pytest.raises(AuthContextError, match="unknown role"):
        parse_auth_context(auth)


def test_non_uuid_user_id_is_rejected(valid_authorizer_context: dict[str, str]) -> None:
    auth = dict(valid_authorizer_context)
    auth["user_id"] = "not-a-uuid"
    with pytest.raises(AuthContextError, match="user_id"):
        parse_auth_context(auth)


def test_non_uuid_member_in_space_ids(valid_authorizer_context: dict[str, str]) -> None:
    auth = dict(valid_authorizer_context)
    auth["space_ids"] = "44444444-4444-4444-8444-444444444444,not-a-uuid"
    with pytest.raises(AuthContextError, match="space_ids"):
        parse_auth_context(auth)


def test_list_of_uuids_for_space_ids(valid_authorizer_context: dict[str, str]) -> None:
    auth = dict(valid_authorizer_context)
    auth["space_ids"] = [  # type: ignore[assignment]
        "44444444-4444-4444-8444-444444444444",
        "55555555-5555-4555-8555-555555555555",
    ]
    ctx = parse_auth_context(auth)
    assert len(ctx.space_ids) == 2


def test_non_mapping_event_raises() -> None:
    with pytest.raises(AuthContextError):
        parse_auth_context("not a dict")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AuthContext API
# ---------------------------------------------------------------------------


def test_to_guc_payload_uses_sorted_comma_join(
    valid_authorizer_context: dict[str, str],
) -> None:
    ctx = parse_auth_context(valid_authorizer_context)
    payload = ctx.to_guc_payload()
    assert payload["current_user_id"] == ctx.user_id
    # space_ids was a 2-member input — sorted join should be lex order.
    assert (
        payload["current_space_ids"]
        == "44444444-4444-4444-8444-444444444444,55555555-5555-4555-8555-555555555555"
    )
    assert payload["current_user_role_set"] == "data_administrator,viewer"


def test_authcontext_is_hashable(valid_authorizer_context: dict[str, str]) -> None:
    ctx = parse_auth_context(valid_authorizer_context)
    # Frozen dataclass with frozensets — must be hashable so it can
    # be used as a memoization key.
    {ctx}  # noqa: B018


def test_has_role_is_case_insensitive(valid_authorizer_context: dict[str, str]) -> None:
    ctx = parse_auth_context(valid_authorizer_context)
    assert ctx.has_role("Viewer") is True
    assert ctx.has_role("VIEWER") is True
    assert ctx.has_role("admin") is False


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


_KNOWN_ROLES = ("admin", "org_admin", "space_admin", "data_administrator", "viewer")


@st.composite
def authorizer_payload_strategy(draw: st.DrawFn) -> dict[str, str | list[str]]:
    """Generate well-formed authorizer payloads for the round-trip property."""
    user_id = str(draw(st.uuids()))
    cognito_sub = str(draw(st.uuids()))
    email_local = draw(st.text(
        alphabet=st.characters(
            min_codepoint=ord("a"),
            max_codepoint=ord("z"),
        ),
        min_size=1,
        max_size=20,
    ))
    email = f"{email_local}@example.org"
    roles = draw(
        st.lists(st.sampled_from(_KNOWN_ROLES), min_size=1, max_size=5, unique=True)
    )
    n_orgs = draw(st.integers(min_value=0, max_value=4))
    n_spaces = draw(st.integers(min_value=0, max_value=6))
    org_uuids = [str(draw(st.uuids())) for _ in range(n_orgs)]
    space_uuids = [str(draw(st.uuids())) for _ in range(n_spaces)]
    return {
        "user_id": user_id,
        "cognito_sub": cognito_sub,
        "email": email,
        "roles": ",".join(roles),
        "org_ids": ",".join(org_uuids),
        "space_ids": ",".join(space_uuids),
    }


@given(authorizer_payload_strategy())
@settings(max_examples=200, deadline=None)
def test_round_trip_through_to_guc_payload(payload: dict[str, str]) -> None:
    """Property: parse(payload).to_guc_payload() preserves the user id and is JSON-safe."""
    ctx = parse_auth_context(payload)

    payload_out = ctx.to_guc_payload()

    # user_id is preserved verbatim
    assert payload_out["current_user_id"] == payload["user_id"]

    # roles are normalized to a sorted-join; reparsing must yield the same set
    reparse_roles = (
        set(payload_out["current_user_role_set"].split(","))
        if payload_out["current_user_role_set"]
        else set()
    )
    expected_roles = set(payload["roles"].split(",")) if payload["roles"] else set()
    assert reparse_roles == expected_roles

    # GUC values are always strings (no None) — required by the
    # migration's `coalesce(current_setting(...), '')` dance.
    for v in payload_out.values():
        assert isinstance(v, str)


@given(authorizer_payload_strategy())
@settings(max_examples=100, deadline=None)
def test_authcontext_construction_total(payload: dict[str, str]) -> None:
    """Any well-formed authorizer payload yields a usable AuthContext."""
    ctx = parse_auth_context(payload)
    assert isinstance(ctx, AuthContext)
    # Privileged check is total
    assert isinstance(ctx.has_role("admin"), bool)

"""Unit tests for biodata_registry_shared.sensitive_flag."""
from __future__ import annotations

import pytest

from biodata_registry_shared.auth_context import AuthContext
from biodata_registry_shared.errors import SensitiveAccessDenied
from biodata_registry_shared.sensitive_flag import check_sensitive_flag


def _ctx(roles: set[str]) -> AuthContext:
    return AuthContext(
        user_id="11111111-1111-4111-8111-111111111111",
        cognito_sub="22222222-2222-4222-8222-222222222222",
        email="alice@example.org",
        org_ids=frozenset(),
        space_ids=frozenset(),
        roles=frozenset(roles),
    )


def test_non_sensitive_asset_is_a_noop() -> None:
    check_sensitive_flag({"sensitive_flag": False}, _ctx({"viewer"}))
    check_sensitive_flag({"is_sensitive": False}, _ctx({"viewer"}))


def test_missing_flag_treated_as_not_sensitive() -> None:
    """When the field is absent, the helper does not block the read."""
    check_sensitive_flag({}, _ctx({"viewer"}))
    check_sensitive_flag({"name": "Subject A"}, _ctx({"viewer"}))


def test_sensitive_asset_blocked_for_non_privileged() -> None:
    with pytest.raises(SensitiveAccessDenied):
        check_sensitive_flag({"sensitive_flag": True}, _ctx({"viewer"}))


def test_sensitive_asset_blocked_for_org_admin() -> None:
    """org_admin is NOT in PRIVILEGED_SENSITIVE_ROLES."""
    with pytest.raises(SensitiveAccessDenied):
        check_sensitive_flag({"is_sensitive": True}, _ctx({"org_admin"}))


def test_sensitive_asset_allowed_for_data_administrator() -> None:
    check_sensitive_flag({"is_sensitive": True}, _ctx({"data_administrator"}))


def test_sensitive_asset_allowed_for_admin() -> None:
    check_sensitive_flag({"sensitive_flag": True}, _ctx({"admin"}))


def test_aurora_field_alias_takes_priority() -> None:
    """If both fields are present, sensitive_flag (Aurora) is checked first."""
    # Aurora doc shape with sensitive_flag = True must block even if
    # is_sensitive=False is also present (defensive against corrupt
    # CDC documents).
    with pytest.raises(SensitiveAccessDenied):
        check_sensitive_flag(
            {"sensitive_flag": True, "is_sensitive": False},
            _ctx({"viewer"}),
        )


def test_non_mapping_input_raises_type_error() -> None:
    with pytest.raises(TypeError):
        check_sensitive_flag("not a mapping", _ctx({"viewer"}))  # type: ignore[arg-type]


def test_truthy_non_bool_treated_as_sensitive() -> None:
    """Defensive: a non-bool truthy value (e.g. CDC sending 1) still blocks."""
    with pytest.raises(SensitiveAccessDenied):
        check_sensitive_flag({"sensitive_flag": 1}, _ctx({"viewer"}))
    with pytest.raises(SensitiveAccessDenied):
        check_sensitive_flag({"is_sensitive": "true"}, _ctx({"viewer"}))

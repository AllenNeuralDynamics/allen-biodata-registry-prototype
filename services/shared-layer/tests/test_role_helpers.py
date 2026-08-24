"""Unit tests for biodata_registry_shared.role_helpers."""
from __future__ import annotations

import pytest

from biodata_registry_shared.auth_context import AuthContext
from biodata_registry_shared.errors import Forbidden
from biodata_registry_shared.role_helpers import (
    DATA_ADMIN_ROLES,
    PRIVILEGED_SENSITIVE_ROLES,
    is_data_admin,
    is_org_admin,
    is_privileged_for_sensitive,
    require_role,
    require_space_access,
)


def _ctx(roles: set[str], *, space_ids: set[str] = frozenset(), org_ids: set[str] = frozenset()) -> AuthContext:
    return AuthContext(
        user_id="11111111-1111-4111-8111-111111111111",
        cognito_sub="22222222-2222-4222-8222-222222222222",
        email="alice@example.org",
        org_ids=frozenset(org_ids),
        space_ids=frozenset(space_ids),
        roles=frozenset(roles),
    )


# ---------------------------------------------------------------------------
# require_role
# ---------------------------------------------------------------------------


def test_require_single_role_passes_when_held() -> None:
    require_role(_ctx({"viewer"}), "viewer")


def test_require_single_role_raises_with_required_role_in_details() -> None:
    with pytest.raises(Forbidden) as exc_info:
        require_role(_ctx({"viewer"}), "org_admin")
    assert exc_info.value.code.value == "FORBIDDEN"
    assert exc_info.value.details == {"required_role": "org_admin"}


def test_require_role_iterable_or_logic() -> None:
    """OR semantics: holding any of the requireds is sufficient."""
    require_role(_ctx({"viewer"}), ["admin", "viewer"])


def test_require_role_iterable_failure_lists_all() -> None:
    with pytest.raises(Forbidden) as exc_info:
        require_role(_ctx({"viewer"}), ["admin", "org_admin"])
    assert exc_info.value.details == {"required_role": ["admin", "org_admin"]}


def test_require_role_normalizes_case() -> None:
    require_role(_ctx({"viewer"}), "VIEWER")


def test_require_role_rejects_empty_iterable() -> None:
    with pytest.raises(ValueError):
        require_role(_ctx({"viewer"}), [])


# ---------------------------------------------------------------------------
# require_space_access
# ---------------------------------------------------------------------------


_SPACE_A = "44444444-4444-4444-8444-444444444444"
_SPACE_B = "55555555-5555-4555-8555-555555555555"


def test_require_space_access_passes_when_member() -> None:
    require_space_access(_ctx({"viewer"}, space_ids={_SPACE_A}), _SPACE_A)


def test_require_space_access_admin_bypasses() -> None:
    """Admins skip Layer 1 because their RLS policy is global-read."""
    require_space_access(_ctx({"admin"}), _SPACE_A)


def test_require_space_access_data_administrator_bypasses() -> None:
    require_space_access(_ctx({"data_administrator"}), _SPACE_A)


def test_require_space_access_raises_for_non_member() -> None:
    with pytest.raises(Forbidden) as exc_info:
        require_space_access(_ctx({"viewer"}, space_ids={_SPACE_B}), _SPACE_A)
    assert exc_info.value.details == {
        "required_role": "space_member",
        "space_id": _SPACE_A,
    }


def test_require_space_access_rejects_empty_space_id() -> None:
    with pytest.raises(ValueError):
        require_space_access(_ctx({"viewer"}), "")


# ---------------------------------------------------------------------------
# is_data_admin / is_org_admin / is_privileged_for_sensitive
# ---------------------------------------------------------------------------


def test_is_data_admin_recognizes_admin_and_data_administrator() -> None:
    assert is_data_admin(_ctx({"admin"})) is True
    assert is_data_admin(_ctx({"data_administrator"})) is True
    assert is_data_admin(_ctx({"viewer"})) is False


def test_is_data_admin_with_mixed_roles() -> None:
    assert is_data_admin(_ctx({"viewer", "admin"})) is True


def test_is_org_admin_without_org_id_filter() -> None:
    assert is_org_admin(_ctx({"org_admin"})) is True
    assert is_org_admin(_ctx({"viewer"})) is False


def test_is_org_admin_scoped_to_org() -> None:
    org_a = "33333333-3333-4333-8333-333333333333"
    org_b = "66666666-6666-4666-8666-666666666666"
    ctx = _ctx({"org_admin"}, org_ids={org_a})
    assert is_org_admin(ctx, org_a) is True
    assert is_org_admin(ctx, org_b) is False


def test_is_privileged_for_sensitive_matches_design() -> None:
    assert is_privileged_for_sensitive(_ctx({"admin"})) is True
    assert is_privileged_for_sensitive(_ctx({"data_administrator"})) is True
    assert is_privileged_for_sensitive(_ctx({"org_admin"})) is False
    assert is_privileged_for_sensitive(_ctx({"viewer"})) is False


# ---------------------------------------------------------------------------
# Role-set sanity
# ---------------------------------------------------------------------------


def test_data_admin_roles_subset_of_known_roles() -> None:
    """Defends against drift between role_helpers and the role_kind enum."""
    from biodata_registry_shared.auth_context import _KNOWN_ROLES  # type: ignore

    assert DATA_ADMIN_ROLES.issubset(_KNOWN_ROLES)
    assert PRIVILEGED_SENSITIVE_ROLES.issubset(_KNOWN_ROLES)

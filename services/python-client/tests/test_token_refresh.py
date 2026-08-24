"""
Tests for :class:`CognitoTokenSource`.

Validates: R15.3 (transparent Cognito token refresh) and the
five-minute expiry skew documented in design.md §External
Interfaces.Python Client and Task 13.2's deliverables.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from biodata_registry_client._token import CognitoTokenSource


def test_returns_cached_token_when_far_from_expiry(make_jwt):
    """A token nowhere near expiry must not trigger a refresh."""
    fresh_token = make_jwt(exp_offset_s=3600)  # 1 hour to go
    cognito = MagicMock()  # no calls expected
    src = CognitoTokenSource(
        cognito_user_pool_id="us-west-2_AbcDef",
        cognito_app_client_id="client-1",
        region="us-west-2",
        refresh_token="refresh-1",
        id_token=fresh_token,
        cognito_client=cognito,
    )

    assert src.get() == fresh_token
    assert src.get() == fresh_token  # still cached
    cognito.initiate_auth.assert_not_called()


def test_refreshes_when_within_skew_window(make_jwt):
    """A token within the 5-minute skew window must trigger a refresh."""
    near_expiry = make_jwt(exp_offset_s=120)  # 2 minutes to go — inside the 5min skew
    new_token = make_jwt(exp_offset_s=3600)
    cognito = MagicMock()
    cognito.initiate_auth.return_value = {
        "AuthenticationResult": {"IdToken": new_token, "ExpiresIn": 3600}
    }

    src = CognitoTokenSource(
        cognito_user_pool_id="us-west-2_AbcDef",
        cognito_app_client_id="client-1",
        region="us-west-2",
        refresh_token="refresh-1",
        id_token=near_expiry,
        cognito_client=cognito,
    )

    out = src.get()
    assert out == new_token
    cognito.initiate_auth.assert_called_once_with(
        ClientId="client-1",
        AuthFlow="REFRESH_TOKEN_AUTH",
        AuthParameters={"REFRESH_TOKEN": "refresh-1"},
    )


def test_refreshes_when_token_already_expired(make_jwt):
    """Past-exp tokens are always replaced before being handed out."""
    expired = make_jwt(exp_offset_s=-60)
    new_token = make_jwt(exp_offset_s=3600)
    cognito = MagicMock()
    cognito.initiate_auth.return_value = {
        "AuthenticationResult": {"IdToken": new_token}
    }

    src = CognitoTokenSource(
        cognito_user_pool_id="us-west-2_AbcDef",
        cognito_app_client_id="client-1",
        region="us-west-2",
        refresh_token="refresh-1",
        id_token=expired,
        cognito_client=cognito,
    )

    assert src.get() == new_token


def test_no_initial_token_forces_refresh_on_first_get(make_jwt):
    """Constructing with refresh_token alone is allowed; first get() refreshes."""
    new_token = make_jwt(exp_offset_s=3600)
    cognito = MagicMock()
    cognito.initiate_auth.return_value = {
        "AuthenticationResult": {"IdToken": new_token}
    }

    src = CognitoTokenSource(
        cognito_user_pool_id="us-west-2_AbcDef",
        cognito_app_client_id="client-1",
        region="us-west-2",
        refresh_token="refresh-1",
        cognito_client=cognito,
    )

    assert src.get() == new_token
    cognito.initiate_auth.assert_called_once()


def test_subsequent_refresh_caches_until_next_skew_window(make_jwt):
    """After a refresh, repeated get() calls don't re-hit Cognito."""
    near = make_jwt(exp_offset_s=10)
    fresh = make_jwt(exp_offset_s=3600)
    cognito = MagicMock()
    cognito.initiate_auth.return_value = {
        "AuthenticationResult": {"IdToken": fresh}
    }
    src = CognitoTokenSource(
        cognito_user_pool_id="pool",
        cognito_app_client_id="client",
        region="us-west-2",
        refresh_token="rt",
        id_token=near,
        cognito_client=cognito,
    )

    src.get()  # forces refresh
    src.get()  # cached
    src.get()  # still cached
    assert cognito.initiate_auth.call_count == 1


def test_missing_refresh_token_raises_when_refresh_needed(make_jwt):
    """Without a refresh_token, an expired token surfaces a runtime error."""
    expired = make_jwt(exp_offset_s=-60)
    src = CognitoTokenSource(
        cognito_user_pool_id="pool",
        cognito_app_client_id="client",
        region="us-west-2",
        refresh_token=None,
        id_token=expired,
    )
    with pytest.raises(RuntimeError, match="no refresh_token"):
        src.get()


def test_constructor_requires_at_least_one_token():
    """Both refresh_token and id_token None is a programming error."""
    with pytest.raises(ValueError, match="at least one of"):
        CognitoTokenSource(
            cognito_user_pool_id="pool",
            cognito_app_client_id="client",
            region="us-west-2",
        )


def test_cognito_response_missing_id_token_is_surfaced(make_jwt):
    """Defensive: an unexpected Cognito response shape doesn't silently cache None."""
    expired = make_jwt(exp_offset_s=-60)
    cognito = MagicMock()
    cognito.initiate_auth.return_value = {"AuthenticationResult": {}}  # no IdToken

    src = CognitoTokenSource(
        cognito_user_pool_id="pool",
        cognito_app_client_id="client",
        region="us-west-2",
        refresh_token="rt",
        id_token=expired,
        cognito_client=cognito,
    )

    with pytest.raises(RuntimeError, match="did not return an IdToken"):
        src.get()


def test_explicit_expiry_override_takes_precedence(make_jwt):
    """When id_token_expires_at is provided, the JWT exp claim is ignored."""
    # Build a JWT that *says* it's valid for 1 hour, but pretend it's
    # expired via the override. The override path is critical for
    # callers who get tokens from custom auth flows (e.g. a SAML
    # gateway that doesn't speak JWT).
    valid_jwt = make_jwt(exp_offset_s=3600)
    new_token = make_jwt(exp_offset_s=3600)
    cognito = MagicMock()
    cognito.initiate_auth.return_value = {
        "AuthenticationResult": {"IdToken": new_token}
    }
    src = CognitoTokenSource(
        cognito_user_pool_id="pool",
        cognito_app_client_id="client",
        region="us-west-2",
        refresh_token="rt",
        id_token=valid_jwt,
        id_token_expires_at=time.time() - 10,  # already expired
        cognito_client=cognito,
    )

    assert src.get() == new_token
    cognito.initiate_auth.assert_called_once()


def test_malformed_jwt_uses_fallback_lifetime():
    """A non-JWT string still produces a usable expiry (conservative fallback)."""
    src = CognitoTokenSource(
        cognito_user_pool_id="pool",
        cognito_app_client_id="client",
        region="us-west-2",
        refresh_token="rt",
        id_token="not-a-jwt",  # no dots, no payload
    )
    # Should be ~50 minutes in the future per the documented fallback.
    assert src.expires_at > time.time() + 30 * 60
    assert src.expires_at < time.time() + 60 * 60


def test_skew_window_zero_means_refresh_on_exact_expiry(make_jwt):
    """Setting skew=0 disables the early-refresh margin; only past-exp triggers refresh."""
    # 1 second to go — inside default 5-min skew, but with skew=0 it's
    # still valid.
    almost_expired = make_jwt(exp_offset_s=10)
    cognito = MagicMock()
    src = CognitoTokenSource(
        cognito_user_pool_id="pool",
        cognito_app_client_id="client",
        region="us-west-2",
        refresh_token="rt",
        id_token=almost_expired,
        refresh_skew_seconds=0,
        cognito_client=cognito,
    )
    src.get()
    cognito.initiate_auth.assert_not_called()

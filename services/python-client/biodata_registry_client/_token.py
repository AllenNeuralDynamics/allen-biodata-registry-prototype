"""
Cognito token-refresh interceptor (R15.3).

The registry's REST API requires an ``Authorization: Bearer <id_token>``
header on every authenticated endpoint. ID tokens are short-lived
(default 60 minutes, configurable per Cognito User Pool client).
:class:`CognitoTokenSource` keeps a cached ID token and silently
refreshes it via Cognito's ``REFRESH_TOKEN_AUTH`` flow when the cache
is within ``refresh_skew_seconds`` of expiry — by default 5 minutes
ahead of the wire ``exp`` claim.

Why the skew matters: without it, a request that takes 60+ seconds
to land at the API Gateway after the client decided the token was
"still valid" would arrive with an expired token. 5 minutes is the
same value AWS internal clients use and gives plenty of margin for
clock skew, slow networks, and retries.

Decoding the ``exp`` claim
--------------------------

Cognito's ``InitiateAuth`` response body does not include an
``ExpiresIn`` for the ID token directly — it returns the access
token expiry, but the ID token can have a separate lifetime. The
robust path is to decode the JWT and read ``exp`` from the payload.
We do this without a JWT library by base64-decoding the middle
segment ourselves: the alternative (``PyJWT``) would pull in
``cryptography`` for verification, but verification is the API
Gateway Authorizer's job, not the client's. The client only needs
to know when to refresh.

If ``exp`` is missing or unparseable, we fall back to a conservative
default of 50 minutes (10 minutes shy of Cognito's 60-minute default
ID token lifetime). This trades one extra refresh per hour for
correctness when the JWT is not in the expected shape.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from typing import Any, Callable, Optional

# boto3 is imported lazily inside _refresh() so that consumers who
# pre-mint and pass an id_token (and never need a refresh) don't pay
# the boto3 import cost. Explicit import-time guards in _refresh()
# raise a clear error if the package is missing on the refresh path.

# Conservative fallback when we can't read ``exp`` from the JWT
# payload. Cognito's default ID token lifetime is 60 minutes; we leave
# a 10-minute safety margin so the next ``REFRESH_TOKEN_AUTH`` call
# happens well before the real expiry.
_FALLBACK_LIFETIME_S = 50 * 60

# Default skew: refresh 5 minutes before expiry. Per the task spec
# ("Cache the token until ~5 minutes before expiry") and matches
# the AWS SDK's default credential-refresh window.
_DEFAULT_REFRESH_SKEW_S = 5 * 60


class CognitoTokenSource:
    """Caches a Cognito ID token and refreshes it on demand.

    Thread-safe: a single source can be shared across threads (e.g.
    by a long-lived service that opens many concurrent clients), and
    only one refresh call will run at a time.

    Parameters
    ----------
    cognito_user_pool_id:
        The pool id, e.g. ``us-west-2_AbcDefGhi``. Stored for
        diagnostics but not used directly by ``REFRESH_TOKEN_AUTH``
        (which is a client-level call).
    cognito_app_client_id:
        The user-pool app client id used for ``REFRESH_TOKEN_AUTH``.
    region:
        AWS region the user pool lives in.
    refresh_token:
        Long-lived Cognito refresh token. Required when
        :attr:`id_token` is not pre-supplied or when an automatic
        refresh is required (i.e. always, in the steady state).
    id_token:
        Optional already-minted ID token. When provided, the source
        starts in a "valid" state and only contacts Cognito on
        expiry.
    id_token_expires_at:
        Optional override for the cached token's expiry. When
        ``None`` and ``id_token`` is provided, the source decodes the
        JWT ``exp`` claim. Useful in tests where the token is a
        fixture string.
    refresh_skew_seconds:
        How far before ``exp`` to consider the token stale.
    cognito_client:
        Optional pre-built ``boto3.client('cognito-idp', ...)``. The
        constructor injects one only when needed; tests inject a
        mock here to exercise the refresh path without boto3.
    time_func:
        Override for ``time.time``; tests inject a fake clock.
    """

    def __init__(
        self,
        *,
        cognito_user_pool_id: str,
        cognito_app_client_id: str,
        region: str,
        refresh_token: Optional[str] = None,
        id_token: Optional[str] = None,
        id_token_expires_at: Optional[float] = None,
        refresh_skew_seconds: int = _DEFAULT_REFRESH_SKEW_S,
        cognito_client: Any = None,
        time_func: Callable[[], float] = time.time,
    ) -> None:
        if id_token is None and refresh_token is None:
            raise ValueError(
                "CognitoTokenSource requires at least one of "
                "id_token or refresh_token; both are None."
            )
        if refresh_skew_seconds < 0:
            raise ValueError("refresh_skew_seconds must be non-negative")

        self._cognito_user_pool_id = cognito_user_pool_id
        self._cognito_app_client_id = cognito_app_client_id
        self._region = region
        self._refresh_token = refresh_token
        self._refresh_skew_seconds = refresh_skew_seconds
        self._cognito_client = cognito_client
        self._time = time_func

        # _lock guards _id_token / _expires_at so concurrent get()
        # calls from multiple threads don't double-refresh and trample
        # each other's cache writes. The lock is held only across the
        # cache check + Cognito call; user code that runs while
        # holding the resulting token never sees the lock.
        self._lock = threading.Lock()
        self._id_token: Optional[str] = id_token
        if id_token is not None:
            self._expires_at = (
                id_token_expires_at
                if id_token_expires_at is not None
                else self._extract_exp(id_token)
            )
        else:
            # No initial token — the next get() forces a refresh.
            self._expires_at = 0.0

    # -----------------------------------------------------------------
    # Public surface used by the client's HTTP adapter.
    # -----------------------------------------------------------------

    def get(self) -> str:
        """Return a non-expired ID token, refreshing if needed.

        Raises
        ------
        RuntimeError
            If a refresh is required but no ``refresh_token`` was
            supplied at construction time.
        botocore.exceptions.ClientError
            Propagated unmodified from boto3 — the caller likely
            wants to surface auth errors as :class:`Unauthorized`,
            but doing so here would couple the token source to the
            HTTP layer.
        """
        with self._lock:
            if self._is_valid_locked():
                # Cached token is still good. Return without contacting
                # Cognito — this is the hot path on every request.
                return self._id_token  # type: ignore[return-value]
            self._refresh_locked()
            assert self._id_token is not None  # narrowed by _refresh_locked
            return self._id_token

    @property
    def expires_at(self) -> float:
        """UNIX timestamp when the cached token's ``exp`` claim says it expires.

        Returns 0 when no token is cached. Exposed for tests and
        instrumentation; not part of the steady-state hot path.
        """
        return self._expires_at

    # -----------------------------------------------------------------
    # Internal helpers (must be called under self._lock).
    # -----------------------------------------------------------------

    def _is_valid_locked(self) -> bool:
        if self._id_token is None:
            return False
        # Refresh strictly before the skew boundary so a request issued
        # right at the boundary still has the full skew_seconds of
        # margin to land at the API Gateway.
        return self._time() < (self._expires_at - self._refresh_skew_seconds)

    def _refresh_locked(self) -> None:
        if self._refresh_token is None:
            raise RuntimeError(
                "Cognito ID token has expired and no refresh_token "
                "was supplied; cannot mint a new token."
            )
        client = self._cognito_client
        if client is None:
            # Lazy boto3 import so non-refreshing callers don't pay
            # the import cost.
            try:
                import boto3  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover - import guard
                raise RuntimeError(
                    "boto3 is required to refresh Cognito tokens; "
                    "install biodata-registry-client[cognito] or "
                    "supply a pre-minted id_token."
                ) from exc
            client = boto3.client("cognito-idp", region_name=self._region)
            # Cache the client for subsequent refreshes — boto3 client
            # construction is non-trivial (a few hundred ms cold).
            self._cognito_client = client

        response = client.initiate_auth(
            ClientId=self._cognito_app_client_id,
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters={"REFRESH_TOKEN": self._refresh_token},
        )
        result = response.get("AuthenticationResult") or {}
        new_id_token = result.get("IdToken")
        if not new_id_token:
            # The shape we expect from a successful refresh — surface
            # the deviation rather than silently caching None.
            raise RuntimeError(
                "Cognito REFRESH_TOKEN_AUTH did not return an IdToken; "
                f"got AuthenticationResult keys: {sorted(result)!r}"
            )
        self._id_token = new_id_token
        self._expires_at = self._extract_exp(new_id_token)

    # -----------------------------------------------------------------
    # JWT exp extraction — pure function, no class state.
    # -----------------------------------------------------------------

    @staticmethod
    def _extract_exp(jwt: str) -> float:
        """Best-effort parse of the JWT ``exp`` claim.

        Returns a UNIX timestamp. Falls back to "now plus
        :data:`_FALLBACK_LIFETIME_S`" when the JWT is missing,
        malformed, or has no ``exp`` claim. This is *deliberately*
        permissive — the client trusts the server's verification, so
        being generous on parsing here just biases us toward more
        frequent refreshes, never toward using stale tokens.

        Implementation note: we don't verify the signature. That's
        the API Gateway Authorizer's job. We just need ``exp``.
        """
        try:
            segments = jwt.split(".")
            if len(segments) < 2:
                raise ValueError("JWT does not have a payload segment")
            payload_b64 = segments[1]
            # JWT uses base64url with optional padding stripped — pad
            # back up to a multiple of 4 before decoding.
            padding = "=" * (-len(payload_b64) % 4)
            payload_bytes = base64.urlsafe_b64decode(payload_b64 + padding)
            payload = json.loads(payload_bytes.decode("utf-8"))
            exp = payload.get("exp")
            if not isinstance(exp, (int, float)):
                raise ValueError("exp claim missing or non-numeric")
            return float(exp)
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            # Conservative fallback — see module docstring.
            return time.time() + _FALLBACK_LIFETIME_S


__all__ = ("CognitoTokenSource",)

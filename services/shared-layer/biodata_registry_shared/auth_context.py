"""
Allen BioData Registry PoC — auth-context parsing.

Every business Lambda receives the user's identity and roles via the
API Gateway custom-authorizer ``context`` field. The Authorizer Lambda
(see Task 15.1) resolves ``user_id``, ``org_ids``, ``space_ids``, and
``roles`` from Aurora on every request (R19.4, R19.5) and returns them
on the IAM policy alongside the standard JWT claims (``sub``, ``email``).

API Gateway flattens this context into a ``dict[str, str]`` — every
value is a string, even integer fields, even arrays. Arrays are
serialized as comma-separated strings because the API Gateway
authorizer protocol does not support nested data. This module is the
single canonical place that round-trips that wire format back into
typed Python.

The module exposes:

* :class:`AuthContext` — frozen dataclass holding the fields every
  downstream component needs.
* :func:`parse_auth_context` — strict parser raising
  :class:`AuthContextError` on missing / malformed input.

Validates: R19.4, R19.5, design.md §Components.1. Authorizer_Lambda.
"""

from __future__ import annotations

import dataclasses
import re
import uuid
from typing import Any, Iterable, Mapping, Sequence

# Permitted role tokens. Mirrors the Postgres ``role_kind`` enum
# created by migration ``0001_governance.sql``. Kept in sync manually
# because the Layer cannot import from the migrations package at
# runtime — drift is caught by tests/test_auth_context.py via a
# regression assertion against this exact set.
_KNOWN_ROLES: frozenset[str] = frozenset(
    {
        "admin",
        "org_admin",
        "space_admin",
        "data_administrator",
        "viewer",
    }
)

# Regex that validates a Cognito ``sub`` claim. Cognito subs are
# UUIDs, but we accept the canonical 8-4-4-4-12 hex form rather than
# requiring the user_id field also be a valid uuid module instance —
# the authorizer might forward the JWT sub as-is even if its
# downstream `user_id` lookup is a database UUID.
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class AuthContextError(ValueError):
    """Raised when the authorizer context cannot be parsed.

    Lifted to its own exception type so handler code can map it to
    HTTP 401 / 403 explicitly. The Authorizer should never produce a
    malformed context, so this firing in production is a signal that
    something is wrong with the upstream Lambda — not user input.
    """


@dataclasses.dataclass(frozen=True, slots=True)
class AuthContext:
    """Parsed authorizer context handed to every business Lambda.

    Attributes
    ----------
    user_id:
        The registry's internal ``app_user.id`` UUID — the **primary
        key** used by RLS policies. Distinct from ``cognito_sub``,
        which is the Cognito-side identifier.
    cognito_sub:
        The ``sub`` claim from the Cognito-issued JWT. Used for
        audit-log attribution and for resolving the user back to
        ``app_user`` if a row is missing.
    email:
        The user's email address (verified at Cognito sign-up).
    org_ids:
        Frozenset of organization UUIDs (strings) the user holds an
        org-level role on. Used for sharing-grant evaluation and to
        seed the ``app.current_org_ids`` GUC.
    space_ids:
        Frozenset of space UUIDs (strings) the user has access to —
        directly assigned via ``user_space_role`` plus inherited via
        org roles plus accessible via active sharing grants. Used to
        seed the ``app.current_space_ids`` GUC and as the OpenSearch
        access filter for non-privileged search.
    roles:
        Frozenset of role tokens (e.g. ``{"viewer", "data_administrator"}``).
        Normalized to lowercase. Members are constrained to the
        :data:`_KNOWN_ROLES` set; anything else raises during parsing.
    raw:
        The original mapping handed to ``parse_auth_context``, kept
        so handlers can extract custom upstream-injected fields
        without re-parsing. Read-only by virtue of the surrounding
        frozen dataclass.

    Notes
    -----
    Iterables are stored as :class:`frozenset` so callers can
    membership-test cheaply (``space_id in ctx.space_ids``) and so
    instances are hashable — useful for memoizing access decisions
    inside a single Lambda invocation.
    """

    user_id: str
    cognito_sub: str
    email: str
    org_ids: frozenset[str]
    space_ids: frozenset[str]
    roles: frozenset[str]
    # ``raw`` is a Mapping so callers can grab arbitrary upstream-injected
    # fields, but we exclude it from hashing/equality. Otherwise the dict
    # value would (a) make AuthContext unhashable and (b) make two
    # otherwise-equal contexts compare unequal because the upstream
    # mapping types differ.
    raw: Mapping[str, Any] = dataclasses.field(
        default_factory=dict,
        compare=False,
        hash=False,
    )

    def has_role(self, role: str) -> bool:
        """Convenience: case-insensitive role check."""
        return role.lower() in self.roles

    def to_guc_payload(self) -> Mapping[str, str]:
        """Materialize the comma-separated strings the RLS GUCs expect.

        The migration ``0006_rls_policies.sql`` reads the GUCs back via
        ``string_to_array(coalesce(current_setting('app.current_*', true), ''), ',')``
        — so empty values must come back as ``''`` (not ``None``) and
        non-empty values must be comma-joined with no surrounding spaces.

        Returns a dict keyed by the GUC names (without the ``app.`` prefix
        — the caller adds it). ``user_role_set`` matches the migration's
        actual GUC name (``app.current_user_role_set``).
        """
        return {
            "current_user_id": self.user_id,
            "current_org_ids": ",".join(sorted(self.org_ids)),
            "current_space_ids": ",".join(sorted(self.space_ids)),
            "current_user_role_set": ",".join(sorted(self.roles)),
        }


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_auth_context(event: Mapping[str, Any]) -> AuthContext:
    """Parse an API Gateway Lambda-proxy event into an :class:`AuthContext`.

    The parser tolerates two event shapes:

    1. **Full Lambda-proxy event** — the dict received by the handler;
       the authorizer context lives at
       ``event["requestContext"]["authorizer"]``.
    2. **Pre-extracted authorizer dict** — the inner authorizer
       payload itself. Useful for unit tests and for nested calls
       (e.g. business Lambdas calling each other in-process).

    All authorizer fields **must** be present and non-empty:

    * ``user_id`` — UUID string, the registry's ``app_user.id``.
    * ``cognito_sub`` (or ``sub``) — UUID string from the JWT.
    * ``email`` — non-empty string.
    * ``roles`` — non-empty comma-separated tokens (or a list).
    * ``space_ids`` — comma-separated UUIDs (or a list); may be empty
      for users who only access ``published`` data.
    * ``org_ids`` — comma-separated UUIDs (or a list); may be empty.

    Raises
    ------
    AuthContextError
        Whenever a required field is missing, malformed (non-UUID
        where a UUID is expected), or contains an unknown role token.
    """
    auth = _extract_authorizer_dict(event)

    user_id = _require_uuid(auth, "user_id")
    cognito_sub = _require_uuid(auth, "cognito_sub", aliases=("sub",))
    email = _require_nonempty_str(auth, "email")

    roles = _coerce_iterable(auth.get("roles"))
    if not roles:
        raise AuthContextError(
            "auth context is missing 'roles' — every authenticated user "
            "must have at least one role"
        )
    normalized_roles = {token.lower() for token in roles}
    unknown_roles = normalized_roles - _KNOWN_ROLES
    if unknown_roles:
        raise AuthContextError(
            f"auth context contains unknown role(s): "
            f"{sorted(unknown_roles)!r}; expected subset of "
            f"{sorted(_KNOWN_ROLES)!r}"
        )

    org_ids = _coerce_uuid_iterable(auth.get("org_ids"), field="org_ids")
    space_ids = _coerce_uuid_iterable(auth.get("space_ids"), field="space_ids")

    return AuthContext(
        user_id=user_id,
        cognito_sub=cognito_sub,
        email=email,
        org_ids=frozenset(org_ids),
        space_ids=frozenset(space_ids),
        roles=frozenset(normalized_roles),
        raw=dict(auth),
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _extract_authorizer_dict(event: Mapping[str, Any]) -> Mapping[str, Any]:
    """Pull the authorizer-context dict out of either event shape."""
    if not isinstance(event, Mapping):
        raise AuthContextError(
            f"expected a mapping (API Gateway event or authorizer dict); "
            f"got {type(event).__name__}"
        )

    request_context = event.get("requestContext")
    if isinstance(request_context, Mapping):
        authorizer = request_context.get("authorizer")
        if isinstance(authorizer, Mapping):
            # API Gateway HTTP API v2 nests the JWT context under a
            # 'jwt' key; REST API v1 puts the context fields at the
            # top level. Support both by merging — top-level fields
            # take priority because that's what the registry's REST
            # authorizer (Task 15.1) emits.
            jwt_payload = authorizer.get("jwt")
            if isinstance(jwt_payload, Mapping):
                claims = jwt_payload.get("claims")
                merged: dict[str, Any] = {}
                if isinstance(claims, Mapping):
                    merged.update(claims)
                merged.update(authorizer)
                return merged
            return authorizer

    # Fall through: assume the event itself IS the authorizer dict.
    return event


def _require_uuid(
    auth: Mapping[str, Any],
    field: str,
    aliases: Sequence[str] = (),
) -> str:
    """Pull a required UUID field, accepting any of the supplied aliases."""
    for name in (field, *aliases):
        raw = auth.get(name)
        if raw is None:
            continue
        if not isinstance(raw, str) or not raw:
            raise AuthContextError(
                f"auth context field {name!r} must be a non-empty string"
            )
        if not _UUID_RE.match(raw):
            # Some upstreams (Cognito's `sub` for SAML federation) are
            # not strictly UUID-shaped; accept anything that is a
            # well-formed UUID via the stdlib parser as a backstop.
            try:
                uuid.UUID(raw)
            except ValueError as exc:
                raise AuthContextError(
                    f"auth context field {name!r} must be a UUID; got {raw!r}"
                ) from exc
        return raw
    raise AuthContextError(
        f"auth context is missing required field {field!r}"
        + (f" (or any of {list(aliases)!r})" if aliases else "")
    )


def _require_nonempty_str(auth: Mapping[str, Any], field: str) -> str:
    raw = auth.get(field)
    if not isinstance(raw, str) or not raw.strip():
        raise AuthContextError(
            f"auth context field {field!r} must be a non-empty string"
        )
    return raw


def _coerce_iterable(value: Any) -> list[str]:
    """Coerce comma-separated string OR list of strings into list[str].

    Empty / None inputs return ``[]`` so the caller can decide whether
    emptiness is acceptable for that particular field.
    """
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [token.strip() for token in value.split(",") if token.strip()]
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
        out: list[str] = []
        for token in value:
            if not isinstance(token, str):
                raise AuthContextError(
                    f"expected list of strings; got element of type "
                    f"{type(token).__name__}"
                )
            stripped = token.strip()
            if stripped:
                out.append(stripped)
        return out
    raise AuthContextError(
        f"expected comma-separated string or list of strings; got "
        f"{type(value).__name__}"
    )


def _coerce_uuid_iterable(value: Any, *, field: str) -> list[str]:
    """As :func:`_coerce_iterable` but additionally validates each member is a UUID."""
    tokens = _coerce_iterable(value)
    for token in tokens:
        if not _UUID_RE.match(token):
            try:
                uuid.UUID(token)
            except ValueError as exc:
                raise AuthContextError(
                    f"auth context field {field!r} contains non-UUID member {token!r}"
                ) from exc
    return tokens


__all__ = (
    "AuthContext",
    "AuthContextError",
    "parse_auth_context",
)

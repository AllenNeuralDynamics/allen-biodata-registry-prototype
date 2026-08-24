"""
Allen BioData Registry PoC — application-level role guards (RLS Layer 1).

Three-layer RLS recap (see design.md §Architecture.RLS Enforcement):

* **Layer 1 — Application:** business Lambdas refuse the request
  before opening a DB connection when the caller's role set doesn't
  authorize the endpoint. This module implements Layer 1.
* **Layer 2 — Database:** the connection helper in :mod:`db` issues
  the ``SET LOCAL app.current_*`` GUCs that drive Postgres RLS
  policies (migration ``0006``). This is the authoritative line.
* **Layer 3 — API/Search:** the Sensitive_Flag check (see
  :mod:`sensitive_flag`) and the OpenSearch filter clause keep
  sensitive rows out of denormalized read paths.

Layer 1 is necessary even though Layer 2 is authoritative because:

1. Some endpoints (``POST /governance/sharing-grants``) gate on
   *capabilities* (only org_admin can create grants) rather than
   on row visibility — RLS happens to also protect the underlying
   row, but failing at Layer 1 returns a clean 403 without a
   wasted DB round-trip.
2. Layer 1 is the cleanest place to surface ``required_role`` in
   the error ``details`` payload (see Error Code Mapping).
3. Defense-in-depth: Property 2 verifies that any **two** of the
   three layers still block sensitive access, so each layer must
   pull its own weight.

Validates: R10.4, R8.5; design.md §Architecture.RLS Enforcement
Architecture (Layer 1).
"""

from __future__ import annotations

from typing import Iterable

from biodata_registry_shared.auth_context import AuthContext
from biodata_registry_shared.errors import Forbidden


# ---------------------------------------------------------------------------
# Role token sets
# ---------------------------------------------------------------------------


# Roles that grant data-administrator-equivalent privileges. The
# ``data_administrator`` role is the explicit name; ``admin`` is the
# system-wide superuser; ``org_admin`` is treated as an admin within
# its org for routing purposes (the per-org scope is enforced by the
# DB-level RLS policies, not by this module).
DATA_ADMIN_ROLES: frozenset[str] = frozenset(
    {"admin", "data_administrator"}
)

# Roles that can read sensitive_flag = true rows. Tracked separately
# from DATA_ADMIN_ROLES because the requirements (R8.2) call out
# data_administrator specifically — and it is plausible that future
# requirements add a "sensitive_data_reviewer" role that has read
# access to sensitive but no other admin powers.
PRIVILEGED_SENSITIVE_ROLES: frozenset[str] = frozenset(
    {"admin", "data_administrator"}
)


# ---------------------------------------------------------------------------
# Public guards
# ---------------------------------------------------------------------------


def require_role(
    auth: AuthContext,
    required: str | Iterable[str],
) -> None:
    """Assert that ``auth`` holds at least one of the required roles.

    ``required`` may be a single role token or any iterable of role
    tokens (membership is OR — passing ``["admin", "org_admin"]``
    means "either is sufficient").

    Raises
    ------
    Forbidden
        With ``details = {"required_role": "<token>"}`` when a single
        role was required, or ``{"required_role": [tokens]}`` for the
        iterable case. Matches the design Error Code Mapping for
        ``FORBIDDEN``.
    """
    required_set = _coerce_role_set(required)

    if not required_set & auth.roles:
        # Render the singleton case as a string so the wire payload
        # exactly matches the design's example (``{"required_role": "org_admin"}``).
        if len(required_set) == 1:
            details = {"required_role": next(iter(required_set))}
        else:
            details = {"required_role": sorted(required_set)}
        raise Forbidden(
            f"Caller is missing required role(s): "
            f"{sorted(required_set)!r}; caller has {sorted(auth.roles)!r}",
            details=details,
        )


def require_space_access(auth: AuthContext, space_id: str) -> None:
    """Assert that ``auth`` has direct access to the given space.

    Layer 1 fast-path: skip the DB query if we know the space isn't
    in the caller's :attr:`~AuthContext.space_ids`. Note that this
    helper checks **direct** access only — sharing-grant-derived
    visibility is computed at Layer 2 by the RLS policies. Lambdas
    that need to permit shared access should defer to Layer 2 by
    issuing the query rather than calling this helper.

    Privileged roles (``admin``, ``data_administrator``) bypass this
    check; their RLS policy is global-read, and refusing them at
    Layer 1 would be inconsistent with what Layer 2 will allow.

    Raises
    ------
    Forbidden
        With ``details = {"required_role": "space_member",
        "space_id": "<uuid>"}`` when the caller is not in the space.
    """
    if not isinstance(space_id, str) or not space_id:
        raise ValueError("space_id must be a non-empty string")

    if is_data_admin(auth):
        return
    if space_id in auth.space_ids:
        return

    raise Forbidden(
        f"Caller does not have direct access to space {space_id!r}",
        details={
            "required_role": "space_member",
            "space_id": space_id,
        },
    )


def is_data_admin(auth: AuthContext) -> bool:
    """True iff the caller holds any of :data:`DATA_ADMIN_ROLES`.

    Used by the Sensitive_Flag check (R8.2) and by Layer 1 guards
    that allow admins to bypass space-membership checks.
    """
    return bool(DATA_ADMIN_ROLES & auth.roles)


def is_org_admin(auth: AuthContext, org_id: str | None = None) -> bool:
    """True iff the caller holds the ``org_admin`` role.

    If ``org_id`` is provided, additionally require the caller has
    that org in their :attr:`AuthContext.org_ids` — matches the
    semantics of the ``sharing_grant_org_admin_policy`` RLS policy
    (org_admins are scoped to their own org).
    """
    if "org_admin" not in auth.roles:
        return False
    if org_id is None:
        return True
    return org_id in auth.org_ids


def is_privileged_for_sensitive(auth: AuthContext) -> bool:
    """True iff the caller may see ``sensitive_flag = true`` rows (R8.2).

    Drawn from :data:`PRIVILEGED_SENSITIVE_ROLES`. Kept as its own
    helper (rather than reusing :func:`is_data_admin`) so the
    sensitive-access role set can evolve independently — a future
    requirement might separate "data administrator" from "sensitive
    data reviewer".
    """
    return bool(PRIVILEGED_SENSITIVE_ROLES & auth.roles)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _coerce_role_set(required: str | Iterable[str]) -> frozenset[str]:
    """Coerce string-or-iterable into a normalized frozenset."""
    if isinstance(required, str):
        return frozenset({required.lower()})
    out: set[str] = set()
    for token in required:
        if not isinstance(token, str) or not token:
            raise ValueError(
                "required role iterable must contain non-empty strings"
            )
        out.add(token.lower())
    if not out:
        raise ValueError("required role set must be non-empty")
    return frozenset(out)


__all__ = (
    "DATA_ADMIN_ROLES",
    "PRIVILEGED_SENSITIVE_ROLES",
    "is_data_admin",
    "is_org_admin",
    "is_privileged_for_sensitive",
    "require_role",
    "require_space_access",
)

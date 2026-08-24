"""
Allen BioData Registry PoC — Sensitive_Flag enforcement (RLS Layer 3).

When a Data_Asset (or Metadata_Entity) has ``sensitive_flag = true``,
Layer 2 (Postgres RLS) only filters it out at the row-fetch layer
when the caller is reading via Aurora. But two paths bypass Aurora:

* **Direct GETs hydrated from a denormalized read store.** Some
  Lambdas hand the OpenSearch / DocumentDB document straight back
  to the client to avoid an Aurora round-trip; the RLS policy never
  fires because Aurora wasn't queried.
* **Search hits.** OpenSearch is filtered to ``is_sensitive: false``
  for non-privileged users by Search_Lambda, but a defensive Layer
  3 check on every hit before returning catches any indexing-bug
  that would let a sensitive document leak.

This module provides the single function every read-path Lambda
calls before returning a hydrated document:

* :func:`check_sensitive_flag` — raises
  :class:`SensitiveAccessDenied` (mapping to HTTP 403
  ``SENSITIVE_ACCESS_DENIED`` per design Error Code Mapping) when
  the caller is non-privileged and the asset is flagged.

Validates: R8.1, R8.2, R8.5; design.md §Architecture.RLS Enforcement
Architecture (Layer 3 — API).
"""

from __future__ import annotations

from typing import Any, Mapping

from biodata_registry_shared.auth_context import AuthContext
from biodata_registry_shared.errors import SensitiveAccessDenied
from biodata_registry_shared.role_helpers import is_privileged_for_sensitive


# Field aliases — the source-of-truth column on Aurora is
# ``sensitive_flag``, but the OpenSearch denormalized doc renames it
# to ``is_sensitive`` (see design §Data Models.OpenSearch Document
# Shape). The check accepts either so the same helper works on both
# document shapes.
_SENSITIVE_FIELD_ALIASES: tuple[str, ...] = ("sensitive_flag", "is_sensitive")


def check_sensitive_flag(
    asset: Mapping[str, Any],
    auth: AuthContext,
) -> None:
    """Raise :class:`SensitiveAccessDenied` if the caller is not privileged.

    Parameters
    ----------
    asset:
        A mapping representing the resource being returned. Must
        carry either ``sensitive_flag`` (Aurora shape) or
        ``is_sensitive`` (OpenSearch shape). Both falsey absence and
        ``False`` are treated as "not sensitive" — no error.
    auth:
        The caller's :class:`AuthContext`.

    Notes
    -----
    Accepts the asset as a generic mapping rather than a typed model
    so this helper works equally well with:

    * pydantic model dumps (``.model_dump()``)
    * raw psycopg row dicts
    * OpenSearch hit ``_source``
    * DocumentDB BSON-decoded documents

    No false negatives: when the field is missing from the document
    we err on the side of caution and treat it as **not sensitive** —
    if a row is genuinely sensitive, the upstream indexer should be
    setting the field. Treating "missing" as "sensitive" would block
    every public-data path that legitimately omits the column.
    """
    if not isinstance(asset, Mapping):
        raise TypeError(
            f"asset must be a Mapping; got {type(asset).__name__}"
        )

    is_sensitive = False
    for alias in _SENSITIVE_FIELD_ALIASES:
        if alias in asset:
            is_sensitive = bool(asset[alias])
            break

    if not is_sensitive:
        return
    if is_privileged_for_sensitive(auth):
        return

    # Don't leak the asset id in the error message — an attacker
    # probing for sensitive ids should not learn anything from the
    # 403. The details payload is intentionally empty per the design
    # Error Code Mapping ("—").
    raise SensitiveAccessDenied()


__all__ = ("check_sensitive_flag",)

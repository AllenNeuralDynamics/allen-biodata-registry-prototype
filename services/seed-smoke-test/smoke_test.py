"""
Allen BioData Registry PoC — seed smoke test core logic.

This module is split out from ``handler.py`` so the assertions can be
unit-tested with a mocked DB cursor, without dragging in the Lambda
framing, IAM token mint, or boto3.

Contract
--------

:func:`run_smoke_test` runs every assertion against the live DB,
collects per-check results (so the operator sees *all* failures, not
just the first), and either returns a :class:`SmokeSummary` or
raises :class:`SmokeTestFailed` when any check failed. The whole
thing is read-only — no INSERT / UPDATE / DELETE — and is safe to
re-invoke.

The aggregate-and-fail-once design is deliberate. A bare-bones
"first failure wins" version would tell the operator "there are zero
data_asset rows" but hide that the FK violations check was *also*
broken — useful debugging context that should not require a second
apply to surface.

What is checked
---------------

1.  ``data_asset`` row count >= ``min_data_assets`` (default 10).
2.  ``subject``    row count >= ``min_subjects``    (default 1).
3.  ``instrument`` row count >= ``min_instruments`` (default 1).
4.  ``session``    row count >= ``min_sessions``    (default 1).
5.  Per-table FK invariants: every child row resolves to a parent.
    The seeder uses real FKs with ``REFERENCES`` clauses, so a true
    orphan would have failed at INSERT time (the seeder rolls back
    the per-record txn on failure). The smoke test therefore
    measures *defense in depth*: a future iteration that disables
    FKs for bulk-load speed would surface here. Tables checked:
       * ``session.data_asset_id`` -> ``data_asset.id``
       * ``acquisition.data_asset_id`` -> ``data_asset.id``
       * ``processing.data_asset_id`` -> ``data_asset.id``
       * ``quality_control.data_asset_id`` -> ``data_asset.id``
       * ``data_description.data_asset_id`` -> ``data_asset.id``
       * each junction row resolves to both parents.
6.  ``data_asset`` rows have non-NULL values in NOT-NULL-but-was-
    defaulted columns (``lifecycle_state``, ``validation_status``,
    ``space_id``, ``created_by``, ``storage_uri``). The defaults in
    migration 0002 mean a NULL here would indicate someone disabled
    the default — defense in depth.
7.  Bootstrap rows exist:
       * ``app_user`` row with ``cognito_sub = 'system-seeder'``.
       * ``organization`` row with ``name = 'system'``.
       * ``space`` row with ``name = 'default-space'`` under that org.
    The seeder creates these idempotently — their absence would
    indicate the bootstrap path was skipped, which would orphan
    every ``created_by`` FK in the seeded data.

Validates
---------

R2.7 (FK constraints prevent orphan references), R32.5 (idempotent
``terraform apply`` — a successful apply guarantees seeded data is
present and consistent).

Design references
-----------------

* design.md §Testing Strategy.E2E Tests.QC1
* design.md §IaC.Idempotency and Sample Data
* migrations/0002_data_asset.sql (column defaults this asserts on)
* migrations/0003_junctions.sql (junction tables this asserts on)
"""

from __future__ import annotations

import dataclasses
import logging
import time
from typing import Any, List, Optional

LOG = logging.getLogger(__name__)


# Bootstrap identifiers — must match seeder.py's constants. We
# duplicate them here rather than importing from the seeder package
# because the smoke test Lambda is a separate deployment artifact and
# should not depend on the seeder's source tree. A tiny risk of drift
# is mitigated by check_bootstrap_rows raising clearly on missing
# rows; if the seeder ever renames a constant, the smoke test fails
# at apply time and the operator updates both sides.
SYSTEM_USER_COGNITO_SUB = "system-seeder"
SYSTEM_ORG_NAME = "system"
DEFAULT_SPACE_NAME = "default-space"


# ---------------------------------------------------------------------------
# Public types.
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class CheckResult:
    """Result of one assertion.

    ``name`` is a stable identifier (used in logs and CloudWatch
    metrics if the operator chooses to alarm on the structured log).
    ``expected`` and ``actual`` are stringified so the dataclass is
    JSON-serialisable; the original numeric / textual form is fine.
    """

    name: str
    expected: str
    actual: str
    passed: bool
    detail: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class SmokeSummary:
    """Result of one ``run_smoke_test`` invocation.

    ``passed`` is the AND of every check's ``passed`` flag. ``checks``
    is the per-check breakdown — operators typically grep CloudWatch
    for ``"passed": false`` to find the first failing check. ``errors``
    captures unexpected exceptions raised while running a check (e.g.
    the table doesn't exist) so they don't lose the rest of the
    smoke test's output.
    """

    passed: bool = True
    checks: List[CheckResult] = dataclasses.field(default_factory=list)
    errors: List[str] = dataclasses.field(default_factory=list)
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": [c.to_dict() for c in self.checks],
            "errors": list(self.errors),
            "elapsed_ms": self.elapsed_ms,
        }


class SmokeTestFailed(RuntimeError):
    """Raised when one or more assertions failed.

    The full :class:`SmokeSummary` is attached as ``.summary`` so the
    handler can surface the per-check breakdown in CloudWatch logs
    even on the failure path.
    """

    def __init__(self, summary: SmokeSummary):
        failing = [c for c in summary.checks if not c.passed]
        msg = (
            f"seed smoke test FAILED: "
            f"{len(failing)}/{len(summary.checks)} checks failed"
        )
        if summary.errors:
            msg += f"; {len(summary.errors)} unexpected errors"
        super().__init__(msg)
        self.summary = summary


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def run_smoke_test(
    *,
    conn: Any,
    min_data_assets: int = 10,
    min_subjects: int = 1,
    min_instruments: int = 1,
    min_sessions: int = 1,
) -> SmokeSummary:
    """Run every smoke check against ``conn``.

    Parameters
    ----------
    conn:
        DB-API 2.0 connection. The smoke test uses ``cursor()`` and
        does NOT commit (every query is read-only). pg8000.dbapi
        conforms; the unit tests use a stateful ``FakeConn``.
    min_data_assets:
        Minimum row count required in ``data_asset``. Default 10 — a
        10% sample of the customer's snapshot produces ~10k rows; 10
        is conservative so the test still passes when an operator
        runs against a smaller sub-sample for development.
    min_subjects, min_instruments, min_sessions:
        Per-table minimums. Default 1 — the smoke test only confirms
        each table has at least one row, not that every Data_Asset
        has a corresponding Subject. The shared-vs-asset-specific
        contract (design.md §Overview.Guiding Principles + Property
        10) means a Data_Asset legitimately may not reference a
        Subject (e.g. a non-experimental dataset).

    Returns
    -------
    A :class:`SmokeSummary` whose ``.passed`` is True iff every check
    passed. If any check failed (or unexpected error occurred), this
    function RAISES :class:`SmokeTestFailed` carrying the summary.
    """
    started = time.monotonic()
    summary = SmokeSummary()

    # Bind a defensive role context so we see all rows even if a
    # future iteration drops to a non-rds_superuser DB user. These
    # GUCs are the inputs to migration 0006_rls_policies.sql's
    # ``is_data_admin()`` helper, which short-circuits the
    # restrictive sensitive-flag policy and bypasses the per-table
    # transitive-visibility predicates. ``SET LOCAL`` constrains the
    # change to the current transaction so it cannot leak.
    _bind_admin_context(conn, summary)

    # Each ``_run_check`` wrapper appends a CheckResult to summary
    # and isolates exceptions so one broken check doesn't cancel the
    # rest. Order matters only for readability in CloudWatch.
    _run_check(
        summary, "data_asset_min_count",
        lambda: _check_table_min_count(conn, "data_asset", min_data_assets),
    )
    _run_check(
        summary, "subject_min_count",
        lambda: _check_table_min_count(conn, "subject", min_subjects),
    )
    _run_check(
        summary, "instrument_min_count",
        lambda: _check_table_min_count(conn, "instrument", min_instruments),
    )
    _run_check(
        summary, "session_min_count",
        lambda: _check_table_min_count(conn, "session", min_sessions),
    )

    # FK orphan checks. Each asset-specific child must resolve to a
    # data_asset; each junction row must resolve to BOTH parents.
    _run_check(
        summary, "session_no_orphan_data_asset",
        lambda: _check_no_orphans(
            conn,
            child_table="session",
            child_fk="data_asset_id",
            parent_table="data_asset",
        ),
    )
    _run_check(
        summary, "acquisition_no_orphan_data_asset",
        lambda: _check_no_orphans(
            conn,
            child_table="acquisition",
            child_fk="data_asset_id",
            parent_table="data_asset",
        ),
    )
    _run_check(
        summary, "processing_no_orphan_data_asset",
        lambda: _check_no_orphans(
            conn,
            child_table="processing",
            child_fk="data_asset_id",
            parent_table="data_asset",
        ),
    )
    _run_check(
        summary, "quality_control_no_orphan_data_asset",
        lambda: _check_no_orphans(
            conn,
            child_table="quality_control",
            child_fk="data_asset_id",
            parent_table="data_asset",
        ),
    )
    _run_check(
        summary, "data_description_no_orphan_data_asset",
        lambda: _check_no_orphans(
            conn,
            child_table="data_description",
            child_fk="data_asset_id",
            parent_table="data_asset",
        ),
    )

    _run_check(
        summary, "data_asset_subject_no_orphan_data_asset",
        lambda: _check_no_orphans(
            conn,
            child_table="data_asset_subject",
            child_fk="data_asset_id",
            parent_table="data_asset",
        ),
    )
    _run_check(
        summary, "data_asset_subject_no_orphan_subject",
        lambda: _check_no_orphans(
            conn,
            child_table="data_asset_subject",
            child_fk="subject_id",
            parent_table="subject",
        ),
    )
    _run_check(
        summary, "data_asset_instrument_no_orphan_data_asset",
        lambda: _check_no_orphans(
            conn,
            child_table="data_asset_instrument",
            child_fk="data_asset_id",
            parent_table="data_asset",
        ),
    )
    _run_check(
        summary, "data_asset_instrument_no_orphan_instrument",
        lambda: _check_no_orphans(
            conn,
            child_table="data_asset_instrument",
            child_fk="instrument_id",
            parent_table="instrument",
        ),
    )

    # Defense-in-depth NULL check on data_asset's defaulted columns.
    _run_check(
        summary, "data_asset_no_null_defaults",
        lambda: _check_no_nulls(
            conn,
            table="data_asset",
            columns=(
                "lifecycle_state",
                "validation_status",
                "space_id",
                "created_by",
                "storage_uri",
            ),
        ),
    )

    # Bootstrap principal rows.
    _run_check(
        summary, "bootstrap_app_user_exists",
        lambda: _check_row_exists(
            conn,
            table="app_user",
            where_col="cognito_sub",
            where_val=SYSTEM_USER_COGNITO_SUB,
        ),
    )
    _run_check(
        summary, "bootstrap_organization_exists",
        lambda: _check_row_exists(
            conn,
            table="organization",
            where_col="name",
            where_val=SYSTEM_ORG_NAME,
        ),
    )
    _run_check(
        summary, "bootstrap_default_space_exists",
        lambda: _check_default_space_exists(conn),
    )

    # Aggregate pass / fail.
    summary.passed = all(c.passed for c in summary.checks) and not summary.errors
    summary.elapsed_ms = int((time.monotonic() - started) * 1000)

    if not summary.passed:
        raise SmokeTestFailed(summary)

    return summary


# ---------------------------------------------------------------------------
# RLS / role context.
# ---------------------------------------------------------------------------


def _bind_admin_context(conn: Any, summary: SmokeSummary) -> None:
    """Set the session GUCs that 0006_rls_policies.sql reads.

    The smoke test connects as ``migration_runner`` which has
    rds_superuser membership and therefore BYPASSRLS. That alone is
    enough to see every row. We additionally set the GUCs because:

    * They are read by views (``subject_viewer_v``) for column-level
      redaction. Without ``data_administrator`` in the role set, the
      view would NULL out ``date_of_birth`` even for the admin user.
      We never SELECT from ``subject_viewer_v`` in the smoke test,
      but it's cheap and defensive to set the GUCs anyway.
    * If a future iteration drops the smoke test to a non-superuser
      DB role, BYPASSRLS will be lost and these GUCs become the
      visibility predicate. Setting them now means that future change
      is a one-line role-rename rather than "rewrite the smoke test".

    ``SET LOCAL`` would be the right scope but pg8000 doesn't open an
    explicit transaction unless we issue BEGIN; ``SET`` (session
    scope) is fine because the connection is single-use and the
    closeout in the handler ends the session. Errors are recorded
    but don't fail the smoke test outright — the assertions will
    fail loudly enough on their own.
    """
    try:
        cur = conn.cursor()
        cur.execute("SET row_security = off")
        cur.execute(
            "SET app.current_user_role_set = 'data_administrator'"
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        msg = f"failed to bind admin context: {exc!r}"
        LOG.warning(msg)
        summary.errors.append(msg)


# ---------------------------------------------------------------------------
# Check primitives.
# ---------------------------------------------------------------------------


def _run_check(
    summary: SmokeSummary,
    name: str,
    fn: Any,
) -> None:
    """Run one check, capture its result, isolate exceptions.

    Each check function returns a ``CheckResult``. If it raises, we
    record the failure as a ``CheckResult(passed=False)`` with the
    exception in ``detail`` and ALSO append the raw error to
    ``summary.errors`` so the operator sees both the contextualised
    failure and the raw stack trace in CloudWatch.
    """
    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001 — see docstring
        msg = f"{name} raised: {exc!r}"
        LOG.exception("smoke check %s raised", name)
        summary.errors.append(msg)
        summary.checks.append(
            CheckResult(
                name=name,
                expected="check ran without raising",
                actual=f"exception: {exc!r}",
                passed=False,
                detail=msg,
            )
        )
        return
    summary.checks.append(result)


def _check_table_min_count(
    conn: Any, table: str, minimum: int
) -> CheckResult:
    """Assert ``SELECT COUNT(*) FROM <table>`` >= ``minimum``.

    The table name is interpolated into the SQL — it is NOT a
    parameter — because Postgres doesn't allow identifiers as bind
    parameters. The values come from this module's hard-coded list,
    so there is no SQL-injection surface; the SELECT is read-only
    regardless.
    """
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    row = cur.fetchone()
    actual = int(row[0]) if row else 0
    return CheckResult(
        name=f"{table}_min_count",
        expected=f">= {minimum}",
        actual=str(actual),
        passed=actual >= minimum,
        detail=(
            None
            if actual >= minimum
            else f"expected at least {minimum} rows in {table}, got {actual}"
        ),
    )


def _check_no_orphans(
    conn: Any,
    *,
    child_table: str,
    child_fk: str,
    parent_table: str,
) -> CheckResult:
    """Assert no row in ``child_table`` has a ``child_fk`` not present in ``parent_table.id``.

    This is the FK-defense-in-depth check. With migrations 0002/0003
    in place the FK constraint already prevents orphans at INSERT
    time, so a non-zero result here means either:
      * someone disabled FKs for bulk-load and forgot to re-enable
        (the seeder doesn't do this, but a future ETL might), or
      * a schema migration accidentally dropped the FK.
    Either way the smoke test surfaces it.

    NULL ``child_fk`` values are excluded — those rows aren't
    orphans, they just don't reference a parent (e.g.
    ``session.subject_id`` is nullable per migration 0002 because
    not every session has a subject).
    """
    sql = (
        f"SELECT COUNT(*) FROM {child_table} c "
        f"WHERE c.{child_fk} IS NOT NULL "
        f"  AND NOT EXISTS ("
        f"    SELECT 1 FROM {parent_table} p WHERE p.id = c.{child_fk}"
        f"  )"
    )
    cur = conn.cursor()
    cur.execute(sql)
    row = cur.fetchone()
    orphans = int(row[0]) if row else 0
    name = f"{child_table}.{child_fk}_no_orphans_in_{parent_table}"
    return CheckResult(
        name=name,
        expected="0 orphans",
        actual=str(orphans),
        passed=orphans == 0,
        detail=(
            None
            if orphans == 0
            else (
                f"{orphans} rows in {child_table}.{child_fk} do not resolve "
                f"to {parent_table}.id"
            )
        ),
    )


def _check_no_nulls(
    conn: Any, *, table: str, columns: tuple[str, ...]
) -> CheckResult:
    """Assert every named column in ``table`` is non-NULL on every row.

    Defense in depth: the columns listed here are NOT NULL in the
    schema (with defaults applied) so a NULL would mean the schema
    drifted. We use a single ``COUNT(*) FILTER (WHERE ... IS NULL OR
    ...)`` over the whole list so the query is one round-trip, not
    one per column.
    """
    if not columns:
        return CheckResult(
            name=f"{table}_no_null_defaults",
            expected="0 nulls",
            actual="0",
            passed=True,
        )
    where_clauses = " OR ".join(f"{col} IS NULL" for col in columns)
    sql = f"SELECT COUNT(*) FROM {table} WHERE {where_clauses}"
    cur = conn.cursor()
    cur.execute(sql)
    row = cur.fetchone()
    null_rows = int(row[0]) if row else 0
    return CheckResult(
        name=f"{table}_no_null_defaults",
        expected="0 nulls",
        actual=str(null_rows),
        passed=null_rows == 0,
        detail=(
            None
            if null_rows == 0
            else (
                f"{null_rows} rows in {table} have NULL in one of: "
                f"{', '.join(columns)}"
            )
        ),
    )


def _check_row_exists(
    conn: Any, *, table: str, where_col: str, where_val: str
) -> CheckResult:
    """Assert ``SELECT 1 FROM <table> WHERE <where_col> = %s LIMIT 1`` returns a row.

    Used for the bootstrap principal rows (``app_user.cognito_sub =
    'system-seeder'`` etc.). Their absence means the seeder's
    bootstrap step was skipped, which would orphan every
    ``created_by`` FK in the seeded data.
    """
    sql = f"SELECT 1 FROM {table} WHERE {where_col} = %s LIMIT 1"
    cur = conn.cursor()
    cur.execute(sql, (where_val,))
    row = cur.fetchone()
    found = row is not None
    return CheckResult(
        name=f"bootstrap_{table}_{where_col}_eq_{where_val}",
        expected="row exists",
        actual="row exists" if found else "row missing",
        passed=found,
        detail=(
            None
            if found
            else (
                f"expected {table}.{where_col} = {where_val!r} (seeder "
                f"bootstrap row); not found"
            )
        ),
    )


def _check_default_space_exists(conn: Any) -> CheckResult:
    """Assert the default-space row exists under the system org.

    The ``space`` table has UNIQUE (org_id, name) so we can't query
    by name alone — multiple orgs could legitimately have a
    ``default-space``. We join through ``organization`` instead.
    """
    sql = (
        "SELECT 1 FROM space s "
        "JOIN organization o ON o.id = s.org_id "
        "WHERE s.name = %s AND o.name = %s "
        "LIMIT 1"
    )
    cur = conn.cursor()
    cur.execute(sql, (DEFAULT_SPACE_NAME, SYSTEM_ORG_NAME))
    row = cur.fetchone()
    found = row is not None
    return CheckResult(
        name="bootstrap_default_space_exists",
        expected="row exists",
        actual="row exists" if found else "row missing",
        passed=found,
        detail=(
            None
            if found
            else (
                f"expected space.name = {DEFAULT_SPACE_NAME!r} under "
                f"organization.name = {SYSTEM_ORG_NAME!r}; not found"
            )
        ),
    )


__all__ = (
    "CheckResult",
    "SmokeSummary",
    "SmokeTestFailed",
    "run_smoke_test",
    "SYSTEM_USER_COGNITO_SUB",
    "SYSTEM_ORG_NAME",
    "DEFAULT_SPACE_NAME",
)

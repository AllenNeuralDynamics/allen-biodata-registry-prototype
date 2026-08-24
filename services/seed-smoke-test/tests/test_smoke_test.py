"""Unit tests for the seed smoke test core logic (``smoke_test.py``).

Tests use a stateful ``FakeConn`` with a programmable response queue
to exercise every assertion path without touching a real Aurora
cluster. The four behaviours required by the task brief are covered:

1. Each assertion correctly identifies the failure mode (paired
   pass / fail tests per check kind).
2. All-pass case returns a successful summary.
3. First-fail-and-aggregate: when multiple checks fail, every failure
   is captured in ``summary.checks`` and the raise happens once at
   the end.
4. Read-only: the smoke test never mutates the DB.
"""

from __future__ import annotations

from typing import Any, List, Tuple

import pytest

from smoke_test import (
    CheckResult,
    SmokeSummary,
    SmokeTestFailed,
    run_smoke_test,
)


# ---------------------------------------------------------------------------
# FakeConn — stateful cursor that pops results from a programmed queue.
# ---------------------------------------------------------------------------


class _FakeCursor:
    """DB-API cursor stub.

    Each ``execute()`` call records the SQL + params; the next
    ``fetchone()`` pops the head of the response queue. The cursor
    supports the context-manager protocol so test setup matches the
    real ``with conn.cursor() as cur:`` pattern in ``smoke_test.py``.
    """

    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._last_result: Any = None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, sql: str, params: Tuple[Any, ...] = ()) -> None:
        self._conn.executed.append((sql, params))
        if self._conn.responses:
            self._last_result = self._conn.responses.pop(0)
        else:
            # No programmed response — default to an empty result. The
            # smoke test treats fetchone() returning None as "0 rows".
            self._last_result = None

    def fetchone(self) -> Any:
        return self._last_result


class _FakeConn:
    """DB-API connection stub.

    ``responses`` is a list of tuples (one per expected fetchone) the
    test sets up before invoking ``run_smoke_test``. The smoke test
    issues one execute → one fetchone per check, in the order the
    checks fire (see ``EXPECTED_CHECK_ORDER`` below).
    """

    def __init__(self, responses: List[Any]) -> None:
        self.responses: List[Any] = list(responses)
        self.executed: List[Tuple[str, Tuple[Any, ...]]] = []
        self.commit_calls = 0
        self.rollback_calls = 0
        self.close_calls = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commit_calls += 1

    def rollback(self) -> None:
        self.rollback_calls += 1

    def close(self) -> None:
        self.close_calls += 1


# Order in which run_smoke_test issues queries. After the two
# admin-context SET statements (which produce no fetchone result —
# the cursor returns None), the data queries run in this fixed order.
# Keeping this list in one place makes "which response feeds which
# check" explicit in the tests.
ADMIN_CONTEXT_QUERY_COUNT = 2
EXPECTED_CHECK_ORDER = [
    "data_asset_min_count",
    "subject_min_count",
    "instrument_min_count",
    "session_min_count",
    "session_no_orphan_data_asset",
    "acquisition_no_orphan_data_asset",
    "processing_no_orphan_data_asset",
    "quality_control_no_orphan_data_asset",
    "data_description_no_orphan_data_asset",
    "data_asset_subject_no_orphan_data_asset",
    "data_asset_subject_no_orphan_subject",
    "data_asset_instrument_no_orphan_data_asset",
    "data_asset_instrument_no_orphan_instrument",
    "data_asset_no_null_defaults",
    "bootstrap_app_user_exists",
    "bootstrap_organization_exists",
    "bootstrap_default_space_exists",
]


def _all_pass_responses(asset_count: int = 100) -> List[Any]:
    """Build a response queue that makes every check pass.

    Two ``None`` entries first for the admin-context SET queries
    (which the smoke test ignores). Then for each check in order:
    a tuple suitable for that check's fetchone consumer.
    """
    return [
        # admin-context SET row_security = off ; SET app.current_user_role_set
        None,
        None,
        # row counts (data_asset, subject, instrument, session)
        (asset_count,),
        (5,),
        (5,),
        (5,),
        # nine FK orphan checks — all return zero
        (0,), (0,), (0,), (0,), (0,),
        (0,), (0,), (0,), (0,),
        # data_asset NULL-defaults check — zero NULLs
        (0,),
        # three bootstrap row-existence checks — all rows present
        (1,), (1,), (1,),
    ]


# ---------------------------------------------------------------------------
# Happy path.
# ---------------------------------------------------------------------------


def test_run_smoke_test_all_pass_returns_summary() -> None:
    conn = _FakeConn(_all_pass_responses())

    summary = run_smoke_test(conn=conn)

    assert isinstance(summary, SmokeSummary)
    assert summary.passed is True
    assert summary.errors == []
    # Every expected check ran exactly once.
    names = [c.name for c in summary.checks]
    # Check order in the summary mirrors the order in
    # EXPECTED_CHECK_ORDER but with the actual generated names.
    assert len(summary.checks) == len(EXPECTED_CHECK_ORDER)
    assert all(c.passed for c in summary.checks)


def test_run_smoke_test_does_not_commit_or_rollback() -> None:
    """Smoke test is read-only — never commits, never rolls back."""
    conn = _FakeConn(_all_pass_responses())

    run_smoke_test(conn=conn)

    assert conn.commit_calls == 0
    assert conn.rollback_calls == 0


def test_run_smoke_test_records_elapsed_ms() -> None:
    conn = _FakeConn(_all_pass_responses())

    summary = run_smoke_test(conn=conn)

    assert summary.elapsed_ms >= 0


# ---------------------------------------------------------------------------
# Per-check failure tests.
# ---------------------------------------------------------------------------


def test_data_asset_min_count_fails_when_below_threshold() -> None:
    responses = _all_pass_responses()
    # Index after the 2 admin-context queries: data_asset_min_count is
    # the next response. Replace with a count of 5 (below default 10).
    responses[ADMIN_CONTEXT_QUERY_COUNT] = (5,)
    conn = _FakeConn(responses)

    with pytest.raises(SmokeTestFailed) as excinfo:
        run_smoke_test(conn=conn, min_data_assets=10)

    summary = excinfo.value.summary
    assert summary.passed is False
    failing = [c for c in summary.checks if not c.passed]
    assert len(failing) == 1
    assert failing[0].name == "data_asset_min_count"
    assert failing[0].expected == ">= 10"
    assert failing[0].actual == "5"
    assert failing[0].detail and "got 5" in failing[0].detail


def test_data_asset_min_count_passes_at_exactly_threshold() -> None:
    """Boundary: count == minimum is a pass (>=, not >)."""
    responses = _all_pass_responses()
    responses[ADMIN_CONTEXT_QUERY_COUNT] = (10,)
    conn = _FakeConn(responses)

    summary = run_smoke_test(conn=conn, min_data_assets=10)

    assert summary.passed is True


def test_subject_min_count_fails_when_zero() -> None:
    """No subjects at all — typical 'seeder didn't insert any subjects' bug."""
    responses = _all_pass_responses()
    # subject_min_count is the second data check (index 3 = 2 admin + 1).
    responses[ADMIN_CONTEXT_QUERY_COUNT + 1] = (0,)
    conn = _FakeConn(responses)

    with pytest.raises(SmokeTestFailed) as excinfo:
        run_smoke_test(conn=conn)

    failing = [c for c in excinfo.value.summary.checks if not c.passed]
    assert any(c.name == "subject_min_count" for c in failing)


def test_instrument_min_count_fails_when_zero() -> None:
    responses = _all_pass_responses()
    responses[ADMIN_CONTEXT_QUERY_COUNT + 2] = (0,)
    conn = _FakeConn(responses)

    with pytest.raises(SmokeTestFailed) as excinfo:
        run_smoke_test(conn=conn)

    failing = [c for c in excinfo.value.summary.checks if not c.passed]
    assert any(c.name == "instrument_min_count" for c in failing)


def test_session_min_count_fails_when_zero() -> None:
    responses = _all_pass_responses()
    responses[ADMIN_CONTEXT_QUERY_COUNT + 3] = (0,)
    conn = _FakeConn(responses)

    with pytest.raises(SmokeTestFailed) as excinfo:
        run_smoke_test(conn=conn)

    failing = [c for c in excinfo.value.summary.checks if not c.passed]
    assert any(c.name == "session_min_count" for c in failing)


def test_session_orphan_check_fails_when_orphans_present() -> None:
    """First FK orphan check: session.data_asset_id has dangling refs."""
    responses = _all_pass_responses()
    # First orphan check is index 4 (after 2 admin + 4 row counts).
    responses[ADMIN_CONTEXT_QUERY_COUNT + 4] = (3,)
    conn = _FakeConn(responses)

    with pytest.raises(SmokeTestFailed) as excinfo:
        run_smoke_test(conn=conn)

    failing = [c for c in excinfo.value.summary.checks if not c.passed]
    orphan_failures = [
        c for c in failing if "no_orphans_in" in c.name and "session" in c.name
    ]
    assert len(orphan_failures) == 1
    assert orphan_failures[0].actual == "3"
    assert orphan_failures[0].expected == "0 orphans"


def test_junction_orphan_subject_check_fails_when_orphans_present() -> None:
    """Junction-table orphan: data_asset_subject.subject_id dangling."""
    responses = _all_pass_responses()
    # data_asset_subject_no_orphan_subject = orphan check index 6
    # (sessions, acquisition, processing, qc, data_description,
    # data_asset_subject->da, data_asset_subject->subject).
    # Position = 2 admin + 4 counts + 6 prior orphans = index 12.
    responses[ADMIN_CONTEXT_QUERY_COUNT + 4 + 6] = (2,)
    conn = _FakeConn(responses)

    with pytest.raises(SmokeTestFailed) as excinfo:
        run_smoke_test(conn=conn)

    failing = [c for c in excinfo.value.summary.checks if not c.passed]
    junction_failures = [
        c
        for c in failing
        if c.name == "data_asset_subject.subject_id_no_orphans_in_subject"
    ]
    assert len(junction_failures) == 1


def test_data_asset_no_null_defaults_check_fails_when_nulls_present() -> None:
    responses = _all_pass_responses()
    # NULL-defaults check is at: 2 admin + 4 counts + 9 orphans = index 15.
    responses[ADMIN_CONTEXT_QUERY_COUNT + 4 + 9] = (7,)
    conn = _FakeConn(responses)

    with pytest.raises(SmokeTestFailed) as excinfo:
        run_smoke_test(conn=conn)

    failing = [c for c in excinfo.value.summary.checks if not c.passed]
    null_failures = [c for c in failing if c.name == "data_asset_no_null_defaults"]
    assert len(null_failures) == 1
    assert null_failures[0].actual == "7"


def test_bootstrap_app_user_check_fails_when_missing() -> None:
    """Bootstrap row missing — system app_user not seeded."""
    responses = _all_pass_responses()
    # Bootstrap app_user check is at: 2 admin + 4 counts + 9 orphans + 1 null = index 16.
    responses[ADMIN_CONTEXT_QUERY_COUNT + 4 + 9 + 1] = None  # row missing
    conn = _FakeConn(responses)

    with pytest.raises(SmokeTestFailed) as excinfo:
        run_smoke_test(conn=conn)

    failing = [c for c in excinfo.value.summary.checks if not c.passed]
    bootstrap_failures = [c for c in failing if "app_user" in c.name]
    assert len(bootstrap_failures) == 1
    assert bootstrap_failures[0].actual == "row missing"


def test_bootstrap_organization_check_fails_when_missing() -> None:
    responses = _all_pass_responses()
    responses[ADMIN_CONTEXT_QUERY_COUNT + 4 + 9 + 2] = None
    conn = _FakeConn(responses)

    with pytest.raises(SmokeTestFailed) as excinfo:
        run_smoke_test(conn=conn)

    failing = [c for c in excinfo.value.summary.checks if not c.passed]
    org_failures = [c for c in failing if "organization" in c.name]
    assert len(org_failures) == 1


def test_bootstrap_default_space_check_fails_when_missing() -> None:
    responses = _all_pass_responses()
    responses[ADMIN_CONTEXT_QUERY_COUNT + 4 + 9 + 3] = None
    conn = _FakeConn(responses)

    with pytest.raises(SmokeTestFailed) as excinfo:
        run_smoke_test(conn=conn)

    failing = [c for c in excinfo.value.summary.checks if not c.passed]
    space_failures = [c for c in failing if "default_space" in c.name]
    assert len(space_failures) == 1


# ---------------------------------------------------------------------------
# Aggregate failure behaviour.
# ---------------------------------------------------------------------------


def test_run_smoke_test_aggregates_multiple_failures_before_raising() -> None:
    """First-fail-and-aggregate: every failure is captured before raise.

    A bare-bones 'first failure wins' design would only report the
    first failed check. We want the operator to see all failures in
    one apply so they can fix them in a single re-run.
    """
    responses = _all_pass_responses()
    # Fail data_asset count, instrument count, AND session orphan check.
    responses[ADMIN_CONTEXT_QUERY_COUNT] = (0,)        # data_asset count
    responses[ADMIN_CONTEXT_QUERY_COUNT + 2] = (0,)    # instrument count
    responses[ADMIN_CONTEXT_QUERY_COUNT + 4] = (5,)    # session orphans > 0
    conn = _FakeConn(responses)

    with pytest.raises(SmokeTestFailed) as excinfo:
        run_smoke_test(conn=conn)

    summary = excinfo.value.summary
    failing = [c for c in summary.checks if not c.passed]
    failing_names = {c.name for c in failing}
    # All three failures present.
    assert "data_asset_min_count" in failing_names
    assert "instrument_min_count" in failing_names
    # The orphan check name is suffixed with "no_orphans_in_data_asset".
    assert any(
        n == "session.data_asset_id_no_orphans_in_data_asset"
        for n in failing_names
    )
    assert len(failing) == 3
    # And the passing checks DID run — aggregate-fail does not abort.
    passing = [c for c in summary.checks if c.passed]
    assert len(passing) == len(EXPECTED_CHECK_ORDER) - 3


def test_run_smoke_test_runs_every_check_even_when_one_raises_unexpectedly() -> None:
    """If one check raises (e.g. table missing), the rest still run.

    A missing table on the FIRST check would cancel the whole
    suite if exceptions weren't isolated per-check. We use a
    response queue with a special sentinel that causes fetchone to
    raise — the test confirms the smoke test records the error and
    keeps running.
    """

    class _RaiseSentinel:
        def __getitem__(self, _: Any) -> Any:  # pragma: no cover
            raise RuntimeError("table data_asset does not exist")

    # Build a FakeConn variant that raises on fetchone for the first
    # data check but otherwise behaves normally.
    class _RaisingFakeConn(_FakeConn):
        def __init__(self, responses: List[Any], raise_at: int) -> None:
            super().__init__(responses)
            self._raise_at = raise_at
            self._calls = 0

        def cursor(self) -> _FakeCursor:
            outer = self

            class _Cur(_FakeCursor):
                def execute(self, sql: str, params: Tuple[Any, ...] = ()) -> None:
                    outer.executed.append((sql, params))
                    outer._calls += 1
                    if outer._calls - 1 == outer._raise_at:
                        raise RuntimeError("table data_asset does not exist")
                    if outer.responses:
                        self._last_result = outer.responses.pop(0)
                    else:
                        self._last_result = None

            return _Cur(outer)

    responses = _all_pass_responses()
    # Drop the response that would have been consumed by the first
    # data check (data_asset count) since we'll raise instead.
    del responses[ADMIN_CONTEXT_QUERY_COUNT]
    conn = _RaisingFakeConn(
        responses,
        raise_at=ADMIN_CONTEXT_QUERY_COUNT,  # raise on first data check
    )

    with pytest.raises(SmokeTestFailed) as excinfo:
        run_smoke_test(conn=conn)

    summary = excinfo.value.summary
    # The error was captured.
    assert any("data_asset_min_count" in e for e in summary.errors)
    # Every check still appears in the summary (the raised one is
    # marked failed).
    assert len(summary.checks) == len(EXPECTED_CHECK_ORDER)
    raised = [c for c in summary.checks if "exception" in (c.actual or "")]
    assert len(raised) == 1
    # And the OTHER 16 checks still passed.
    assert sum(1 for c in summary.checks if c.passed) == len(EXPECTED_CHECK_ORDER) - 1


def test_smoke_test_failed_carries_summary_attribute() -> None:
    responses = _all_pass_responses()
    responses[ADMIN_CONTEXT_QUERY_COUNT] = (0,)  # force one failure
    conn = _FakeConn(responses)

    with pytest.raises(SmokeTestFailed) as excinfo:
        run_smoke_test(conn=conn)

    assert hasattr(excinfo.value, "summary")
    assert isinstance(excinfo.value.summary, SmokeSummary)
    assert excinfo.value.summary.passed is False


# ---------------------------------------------------------------------------
# Threshold parameterisation.
# ---------------------------------------------------------------------------


def test_thresholds_are_independently_configurable() -> None:
    """Calling with elevated thresholds correctly fails when not met."""
    responses = _all_pass_responses(asset_count=50)
    conn = _FakeConn(responses)

    # 50 assets is fine for default min_data_assets=10 but not for 100.
    with pytest.raises(SmokeTestFailed) as excinfo:
        run_smoke_test(
            conn=conn,
            min_data_assets=100,
            min_subjects=1,
            min_instruments=1,
            min_sessions=1,
        )

    failing = [c for c in excinfo.value.summary.checks if not c.passed]
    assert len(failing) == 1
    assert failing[0].name == "data_asset_min_count"
    assert failing[0].expected == ">= 100"
    assert failing[0].actual == "50"


def test_data_asset_min_count_passes_when_well_above_threshold() -> None:
    responses = _all_pass_responses(asset_count=10_000)
    conn = _FakeConn(responses)

    summary = run_smoke_test(conn=conn, min_data_assets=10)

    assert summary.passed is True


# ---------------------------------------------------------------------------
# CheckResult / SmokeSummary serialization.
# ---------------------------------------------------------------------------


def test_summary_to_dict_is_json_serialisable() -> None:
    """The summary returned by the handler MUST be JSON-serialisable —
    Terraform's aws_lambda_invocation captures it as ``result``."""
    import json

    responses = _all_pass_responses()
    conn = _FakeConn(responses)

    summary = run_smoke_test(conn=conn)
    serialised = json.dumps(summary.to_dict())

    assert "checks" in serialised
    assert '"passed": true' in serialised


def test_check_result_to_dict_includes_all_fields() -> None:
    cr = CheckResult(
        name="example",
        expected="0",
        actual="0",
        passed=True,
        detail=None,
    )
    d = cr.to_dict()
    assert d["name"] == "example"
    assert d["passed"] is True
    assert "detail" in d

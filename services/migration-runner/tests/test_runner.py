"""Unit tests for the Migration Runner core logic (``runner.py``).

These tests exercise ``runner.run_migrations`` against a stateful fake
DB connection (``FakeConn``) that:

* Tracks whether the ``schema_version`` table exists.
* Honours INSERTs into ``schema_version`` so subsequent calls see the
  row.
* Records every executed statement so the tests can assert on order,
  transaction shape, and content.

The fake intentionally does not parse SQL — the runner's contract with
its driver is that a successful ``cursor.execute`` is the same as a
successful Aurora apply. Tests therefore look at *what the runner
asked the DB to do*, not at the SQL semantics. Real SQL semantics are
exercised by the (out-of-scope-for-this-task) end-to-end Aurora apply
in Task 10.

Tests required by Task 8.1:

1. **First invocation creates schema_version + applies all 7 migrations.**
2. **Second invocation is a no-op.**
3. **Modified file (different checksum) is reported as drift but NOT re-applied.**
4. **Out-of-order discovery raises** (``MigrationOrderError``).

Plus property-based coverage for the file-discovery and version-parse helpers.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

import runner


# ---------------------------------------------------------------------------
# FakeConn — in-memory DB-API 2.0-shaped stand-in.
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _ExecCall:
    sql: str
    params: Optional[Tuple[Any, ...]]
    autocommit: bool


class _FakeCursor:
    """Cursor that interprets just enough SQL to make the runner think
    it is talking to a real Postgres."""

    def __init__(self, conn: "FakeConn") -> None:
        self._conn = conn
        self._fetch_buffer: List[Tuple[Any, ...]] = []

    # Context-manager support — the runner uses `with conn.cursor() as cur:`.
    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False

    def execute(self, sql: str, params: Optional[Tuple[Any, ...]] = None) -> None:
        # Record what the runner asked for, including current autocommit
        # mode at call time — the no-transaction test asserts on this.
        self._conn.calls.append(
            _ExecCall(sql=sql, params=params, autocommit=self._conn.autocommit)
        )

        normalized = " ".join(sql.split()).lower()

        # Existence probe.
        if "from information_schema.tables" in normalized and "schema_version" in normalized:
            self._fetch_buffer = [(1,)] if self._conn.schema_version_exists else []
            return

        # CREATE TABLE schema_version — make the table exist henceforth.
        if "create table" in normalized and "schema_version" in normalized:
            self._conn.schema_version_exists = True
            return

        # SELECT applied migrations.
        if "select version, filename, checksum from schema_version" in normalized:
            self._fetch_buffer = [
                (v, row["filename"], row["checksum"])
                for v, row in sorted(self._conn.applied_rows.items())
            ]
            return

        # INSERT INTO schema_version (...).
        if "insert into schema_version" in normalized:
            assert params is not None and len(params) == 4, (
                "Runner must pass (version, filename, checksum, applied_by) to the INSERT"
            )
            version, filename, checksum, applied_by = params
            # Real Postgres would raise on duplicate-PK INSERT. The
            # runner's contract guarantees it never INSERTs an existing
            # version, so we mirror that and assert here too.
            assert version not in self._conn.applied_rows, (
                f"runner attempted to re-INSERT version={version!r}"
            )
            self._conn.applied_rows[version] = {
                "filename": filename,
                "checksum": checksum,
                "applied_by": applied_by,
            }
            return

        # Anything else (the migration body): record the apply.
        self._conn.executed_bodies.append(sql)

    def fetchone(self) -> Optional[Tuple[Any, ...]]:
        return self._fetch_buffer.pop(0) if self._fetch_buffer else None

    def fetchall(self) -> List[Tuple[Any, ...]]:
        out, self._fetch_buffer = self._fetch_buffer, []
        return out


class FakeConn:
    """DB-API 2.0-shaped in-memory connection.

    Only models what ``runner.py`` needs:

    * ``cursor()`` returning a context-manager cursor.
    * ``commit()`` / ``rollback()`` counters.
    * ``autocommit`` writable attribute.
    """

    def __init__(self) -> None:
        self.schema_version_exists: bool = False
        self.applied_rows: Dict[str, Dict[str, Any]] = {}
        self.calls: List[_ExecCall] = []
        self.executed_bodies: List[str] = []
        self.commit_count: int = 0
        self.rollback_count: int = 0
        self.autocommit: bool = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _write_migration(dir_: Path, name: str, body: str) -> Path:
    p = dir_ / name
    p.write_text(body, encoding="utf-8")
    return p


def _seven_registry_migrations(dir_: Path) -> List[str]:
    """Create dummy SQL files matching the registry's 7-migration shape.

    The bodies are different per file so checksums differ — important
    for the drift test.
    """
    names = [
        "0001_governance.sql",
        "0002_data_asset.sql",
        "0003_junctions.sql",
        "0004_revisions_lifecycle_duplicates.sql",
        "0005_collections_schemas.sql",
        "0006_rls_policies.sql",
        "0007_search_indexes.sql",
    ]
    for n in names:
        _write_migration(dir_, n, f"-- migration {n}\nSELECT 1; -- {n}\n")
    return names


# ---------------------------------------------------------------------------
# 1) First invocation creates schema_version + applies all 7 migrations.
# ---------------------------------------------------------------------------


def test_first_invocation_creates_schema_version_and_applies_all(tmp_path: Path) -> None:
    names = _seven_registry_migrations(tmp_path)
    conn = FakeConn()

    summary = runner.run_migrations(
        conn=conn,
        migrations_dir=tmp_path,
        applied_by="migration_runner",
    )

    # Behavior contract.
    assert summary.schema_version_created is True
    assert summary.applied == names
    assert summary.skipped == []
    assert summary.drift == []

    # Every migration was recorded.
    assert sorted(conn.applied_rows.keys()) == [f"000{i}" for i in range(1, 8)]

    # Each row carries the IAM DB user as applied_by.
    assert all(row["applied_by"] == "migration_runner" for row in conn.applied_rows.values())

    # Each migration body was executed exactly once and in lexical order.
    assert len(conn.executed_bodies) == 7
    for executed_sql, name in zip(conn.executed_bodies, names):
        assert name in executed_sql, f"body for {name} not executed in order"

    # Default mode wraps each apply in an explicit transaction —
    # autocommit must be False for every body execution and INSERT.
    body_calls = [c for c in conn.calls if c.sql in conn.executed_bodies]
    assert all(not c.autocommit for c in body_calls)


def test_first_invocation_records_filename_and_checksum(tmp_path: Path) -> None:
    names = _seven_registry_migrations(tmp_path)
    conn = FakeConn()

    runner.run_migrations(conn=conn, migrations_dir=tmp_path, applied_by="x")

    for name in names:
        version = name.split("_")[0]
        row = conn.applied_rows[version]
        assert row["filename"] == name
        # Runner stored the SHA-256 of the file contents.
        expected = runner.compute_checksum(tmp_path / name)
        assert row["checksum"] == expected


# ---------------------------------------------------------------------------
# 2) Second invocation is a no-op.
# ---------------------------------------------------------------------------


def test_second_invocation_is_a_no_op(tmp_path: Path) -> None:
    names = _seven_registry_migrations(tmp_path)
    conn = FakeConn()

    # First run.
    runner.run_migrations(conn=conn, migrations_dir=tmp_path, applied_by="x")

    # Reset the apply-tracker but KEEP the simulated DB state. This
    # matches what happens between two `terraform apply` invocations:
    # the Lambda is a fresh container, but the DB already has the rows.
    conn.executed_bodies.clear()
    conn.calls.clear()
    conn.commit_count = 0

    summary = runner.run_migrations(conn=conn, migrations_dir=tmp_path, applied_by="x")

    # Schema_version already existed: no creation, no applies, all skipped.
    assert summary.schema_version_created is False
    assert summary.applied == []
    assert summary.skipped == names
    assert summary.drift == []

    # No migration body was re-executed.
    assert conn.executed_bodies == []


# ---------------------------------------------------------------------------
# 3) Drift: modified file is reported but NOT re-applied.
# ---------------------------------------------------------------------------


def test_modified_file_is_reported_as_drift_and_not_reapplied(tmp_path: Path) -> None:
    names = _seven_registry_migrations(tmp_path)
    conn = FakeConn()

    runner.run_migrations(conn=conn, migrations_dir=tmp_path, applied_by="x")

    # Now mutate one of the applied files. Forward-only convention says
    # this is wrong — the runner must surface the drift loudly without
    # re-applying.
    drifted = tmp_path / "0003_junctions.sql"
    drifted.write_text("-- mutated body\nSELECT 'drift';\n", encoding="utf-8")
    new_checksum = runner.compute_checksum(drifted)

    conn.executed_bodies.clear()
    conn.calls.clear()

    summary = runner.run_migrations(conn=conn, migrations_dir=tmp_path, applied_by="x")

    assert summary.applied == []
    # Drifted file is still in `skipped` (the runner did not re-apply).
    assert "0003_junctions.sql" in summary.skipped
    # And it is reported in the drift list.
    assert len(summary.drift) == 1
    drift = summary.drift[0]
    assert drift.filename == "0003_junctions.sql"
    assert drift.version == "0003"
    assert drift.actual_checksum == new_checksum
    # The expected_checksum is what was originally recorded.
    assert drift.expected_checksum is not None
    assert drift.expected_checksum != new_checksum

    # Critically: the new body was NOT executed.
    assert all("drift" not in body for body in conn.executed_bodies)
    assert conn.executed_bodies == []


def test_drift_with_one_new_migration_still_applies_the_new_one(tmp_path: Path) -> None:
    """A drifted file should NOT block apply of subsequent new files —
    the runner reports drift on the old file and still applies new ones."""
    _seven_registry_migrations(tmp_path)
    conn = FakeConn()
    runner.run_migrations(conn=conn, migrations_dir=tmp_path, applied_by="x")

    # Mutate file 0002 AND add a new file 0008.
    (tmp_path / "0002_data_asset.sql").write_text("-- mutated 0002\n", encoding="utf-8")
    _write_migration(tmp_path, "0008_new_thing.sql", "-- new 0008\nSELECT 1;\n")

    conn.executed_bodies.clear()
    summary = runner.run_migrations(conn=conn, migrations_dir=tmp_path, applied_by="x")

    assert summary.applied == ["0008_new_thing.sql"]
    assert len(summary.drift) == 1
    assert summary.drift[0].filename == "0002_data_asset.sql"


# ---------------------------------------------------------------------------
# 4) Out-of-order discovery raises.
# ---------------------------------------------------------------------------


def test_out_of_order_new_migration_raises(tmp_path: Path) -> None:
    """If 0008 is already applied and 0007 shows up new on disk, refuse."""
    # Apply 0007 + 0008 first.
    _write_migration(tmp_path, "0007_search.sql", "-- 0007\n")
    _write_migration(tmp_path, "0008_indexes.sql", "-- 0008\n")
    conn = FakeConn()
    runner.run_migrations(conn=conn, migrations_dir=tmp_path, applied_by="x")

    # Now drop a new 0006 (older than the latest applied 0008).
    _write_migration(tmp_path, "0006_oops.sql", "-- 0006\n")

    with pytest.raises(runner.MigrationOrderError) as exc_info:
        runner.run_migrations(conn=conn, migrations_dir=tmp_path, applied_by="x")

    assert "0006_oops.sql" in str(exc_info.value)
    # Importantly: the order error fires BEFORE applying anything, so
    # no new bodies were executed.
    conn.executed_bodies.clear()  # reset (the first run executed bodies)
    # Re-run and confirm no body executes prior to the raise.
    with pytest.raises(runner.MigrationOrderError):
        runner.run_migrations(conn=conn, migrations_dir=tmp_path, applied_by="x")
    assert conn.executed_bodies == []


def test_filename_violation_raises(tmp_path: Path) -> None:
    """Non-conforming filenames are rejected up front."""
    _write_migration(tmp_path, "not_a_migration.sql", "-- nope\n")
    conn = FakeConn()
    with pytest.raises(runner.MigrationFilenameError):
        runner.run_migrations(conn=conn, migrations_dir=tmp_path, applied_by="x")


def test_missing_directory_raises(tmp_path: Path) -> None:
    bogus = tmp_path / "does-not-exist"
    conn = FakeConn()
    with pytest.raises(runner.MigrationRunnerError):
        runner.run_migrations(conn=conn, migrations_dir=bogus, applied_by="x")


# ---------------------------------------------------------------------------
# No-transaction directive.
# ---------------------------------------------------------------------------


def test_no_transaction_directive_runs_in_autocommit_mode(tmp_path: Path) -> None:
    """``-- +runner: no-transaction`` opts a file out of the BEGIN/COMMIT wrap."""
    _write_migration(
        tmp_path,
        "0001_no_tx.sql",
        "-- +runner: no-transaction\nCREATE INDEX CONCURRENTLY foo_idx ON bar (baz);\n",
    )
    conn = FakeConn()

    runner.run_migrations(conn=conn, migrations_dir=tmp_path, applied_by="x")

    body_calls = [c for c in conn.calls if c.sql in conn.executed_bodies]
    assert len(body_calls) == 1
    # The body executed with autocommit ON.
    assert body_calls[0].autocommit is True
    # And autocommit was restored to False afterwards.
    assert conn.autocommit is False


def test_no_transaction_directive_only_recognized_in_first_100_chars(tmp_path: Path) -> None:
    """A directive buried deeper in the file must NOT toggle the mode."""
    body = "-- a normal comment\n" + ("-- filler\n" * 50) + "-- +runner: no-transaction\nSELECT 1;\n"
    _write_migration(tmp_path, "0001_buried.sql", body)
    conn = FakeConn()

    runner.run_migrations(conn=conn, migrations_dir=tmp_path, applied_by="x")

    body_calls = [c for c in conn.calls if c.sql in conn.executed_bodies]
    assert body_calls[0].autocommit is False


# ---------------------------------------------------------------------------
# Helper-function coverage (parse_version, discover_migration_files).
# ---------------------------------------------------------------------------


def test_parse_version_extracts_numeric_prefix() -> None:
    assert runner.parse_version("0001_governance.sql") == "0001"
    assert runner.parse_version("0042_thing.sql") == "0042"
    assert runner.parse_version("12345_big.sql") == "12345"


def test_parse_version_rejects_non_conforming_names() -> None:
    for name in ["governance.sql", "abc_thing.sql", "001.sql.bak", "0001-no-underscore.sql"]:
        with pytest.raises(runner.MigrationFilenameError):
            runner.parse_version(name)


def test_discover_migration_files_returns_lexical_order(tmp_path: Path) -> None:
    # Create out of order on disk.
    for n in ["0003_c.sql", "0001_a.sql", "0002_b.sql"]:
        _write_migration(tmp_path, n, f"-- {n}\n")
    assert list(runner.discover_migration_files(tmp_path)) == [
        "0001_a.sql",
        "0002_b.sql",
        "0003_c.sql",
    ]


# ---------------------------------------------------------------------------
# Property-based: arbitrary filename corpora apply in lexical order.
# ---------------------------------------------------------------------------


_VERSION = st.from_regex(r"\d{4}", fullmatch=True)
_SLUG = st.from_regex(r"[a-z][a-z0-9_]{0,15}", fullmatch=True)


@st.composite
def _migration_filenames(draw: Any) -> List[str]:
    """Generate 1-15 unique zero-padded filenames."""
    pairs = draw(
        st.lists(
            st.tuples(_VERSION, _SLUG),
            min_size=1,
            max_size=15,
            unique_by=lambda p: p[0],
        )
    )
    return [f"{v}_{s}.sql" for v, s in pairs]


@given(filenames=_migration_filenames())
@settings(
    max_examples=40,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
def test_property_run_migrations_applies_in_lexical_order(
    filenames: List[str], tmp_path_factory: pytest.TempPathFactory
) -> None:
    """For any valid filename corpus, ``run_migrations`` applies the
    files in lexical order on the first invocation and is a no-op on
    the second.

    Validates: R32.5 (idempotent ``terraform apply``).
    """
    d = tmp_path_factory.mktemp("migrations")
    for n in filenames:
        _write_migration(d, n, f"-- body for {n}\n")

    conn = FakeConn()

    summary1 = runner.run_migrations(conn=conn, migrations_dir=d, applied_by="x")
    expected = sorted(filenames)
    assert summary1.applied == expected
    assert summary1.skipped == []
    assert summary1.drift == []

    # Idempotency: re-running with the same disk + DB state does nothing.
    summary2 = runner.run_migrations(conn=conn, migrations_dir=d, applied_by="x")
    assert summary2.applied == []
    assert summary2.skipped == expected
    assert summary2.drift == []

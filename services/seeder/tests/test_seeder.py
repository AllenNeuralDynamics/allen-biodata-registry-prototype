"""Unit tests for the Seeder core logic (``seeder.py``).

These tests exercise ``seeder.run_seeder`` against:

* a stateful in-memory ``FakeConn`` that interprets just enough SQL to
  honour the seeder's INSERT/UPSERT contract (UNIQUE constraints,
  ON CONFLICT DO NOTHING / DO UPDATE RETURNING id, composite PKs on
  junctions),
* a ``FakeS3Client`` that returns the canonical 5-record fixture.

Tests required by Task 9.1:

1. **Mapping logic produces correct INSERTs for a 5-record fixture** —
   exercised end-to-end through ``run_seeder`` so we verify the full
   pipeline (S3 → mapping → DB), not just the mapper in isolation.
2. **Idempotency** — re-running ``run_seeder`` against the same
   fixture and the same FakeConn (state carried across) produces
   zero new INSERTs.
3. **Sampling fraction is deterministic** — running with
   ``sample_fraction=0.4`` selects the same record subset on every
   run.
4. **Missing optional fields don't crash** — the minimal record in
   the fixture lands cleanly with no shared entities and no children.
"""

from __future__ import annotations

import io
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

import mapping
import seeder


_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_records.json"


# ---------------------------------------------------------------------------
# FakeConn — interprets enough Postgres dialect to make run_seeder happy.
# ---------------------------------------------------------------------------


@dataclass
class _Table:
    """Represents one table in the fake DB.

    ``unique_cols`` lists single-column UNIQUE indexes used by ON
    CONFLICT (name) DO ... clauses. Composite primary keys
    (junctions) are tracked via ``composite_pk_cols``.
    """

    columns: List[str]
    unique_cols: List[str] = field(default_factory=list)
    composite_pk_cols: Optional[Tuple[str, ...]] = None
    rows: List[Dict[str, Any]] = field(default_factory=list)
    next_id: int = 1


_INSERT_RE = re.compile(
    r"INSERT\s+INTO\s+(?P<table>\w+)\s*\((?P<cols>[^)]+)\)\s+VALUES\s*\((?P<placeholders>[^)]+)\)"
    r"(?P<rest>.*)",
    re.DOTALL | re.IGNORECASE,
)
_ON_CONFLICT_TARGET_RE = re.compile(
    r"ON\s+CONFLICT\s*\((?P<col>[^)]+)\)", re.IGNORECASE
)
_ON_CONFLICT_DO_NOTHING_RE = re.compile(
    r"ON\s+CONFLICT[^D]*DO\s+NOTHING", re.IGNORECASE
)
_ON_CONFLICT_DO_UPDATE_RE = re.compile(
    r"ON\s+CONFLICT.*?DO\s+UPDATE", re.IGNORECASE | re.DOTALL
)
_RETURNING_RE = re.compile(r"RETURNING\s+(?P<cols>.+?)$", re.IGNORECASE)
_SELECT_RE = re.compile(
    r"^\s*SELECT\s+id\s+FROM\s+(?P<table>\w+)\s+WHERE\s+(?P<col>\w+)\s*=\s*%s\s*$",
    re.IGNORECASE,
)
_UPSERT_CTE_RE = re.compile(
    r"WITH\s+upserted\s+AS\s*\(\s*"
    r"INSERT\s+INTO\s+(?P<table>\w+)\s*\((?P<cols>[^)]+)\)\s+VALUES\s*\((?P<placeholders>[^)]+)\)\s+"
    r"ON\s+CONFLICT\s*\((?P<conflict>\w+)\)\s+DO\s+UPDATE\s+SET\s+\w+\s*=\s*EXCLUDED\.\w+\s+"
    r"RETURNING\s+id,\s*\(\s*xmax\s*=\s*0\s*\)\s+AS\s+inserted\s*\)\s+"
    r"SELECT\s+id,\s*inserted\s+FROM\s+upserted\s*$",
    re.IGNORECASE | re.DOTALL,
)


def _build_fake_schema() -> Dict[str, _Table]:
    """Construct the subset of registry tables the seeder writes to."""
    return {
        "organization": _Table(
            columns=["id", "name", "display_name", "created_at"],
            unique_cols=["name"],
        ),
        "space": _Table(
            columns=[
                "id", "org_id", "name", "display_name",
                "parent_space_id", "created_at",
            ],
            # composite UNIQUE (org_id, name) — we synthesise a key-pair
            # uniqueness via two-column comparison.
            unique_cols=["__org_name__"],
        ),
        "app_user": _Table(
            columns=["id", "cognito_sub", "email", "org_id", "created_at"],
            unique_cols=["cognito_sub"],
        ),
        "data_asset": _Table(
            columns=[
                "id", "space_id", "name", "display_name", "storage_uri",
                "data_type", "lifecycle_state", "validation_status",
                "schema_version", "description", "metadata", "created_by",
                "created_at",
            ],
            unique_cols=["storage_uri"],
        ),
        "subject": _Table(
            columns=[
                "id", "subject_id", "species", "sex", "date_of_birth",
                "genotype", "source", "weight_at_acquisition_g",
                "age_at_acquisition_days", "notes", "metadata",
                "created_by", "created_at",
            ],
            unique_cols=["subject_id"],
        ),
        "instrument": _Table(
            columns=[
                "id", "instrument_id", "instrument_type", "manufacturer",
                "model", "serial_number", "calibration_date", "notes",
                "metadata", "created_by", "created_at",
            ],
            unique_cols=["instrument_id"],
        ),
        "rig": _Table(
            columns=[
                "id", "rig_id", "modalities", "location", "notes",
                "metadata", "created_by", "created_at",
            ],
            unique_cols=["rig_id"],
        ),
        "procedures": _Table(
            columns=[
                "id", "subject_id", "surgery_date", "protocol",
                "performed_by", "notes", "metadata", "created_by",
                "created_at",
            ],
        ),
        "session": _Table(
            columns=[
                "id", "data_asset_id", "session_id", "session_type",
                "session_start", "session_end", "experimenter",
                "subject_id", "instrument_id", "rig_id", "notes",
                "metadata", "created_at",
            ],
        ),
        "acquisition": _Table(
            columns=[
                "id", "data_asset_id", "session_id", "instrument_id",
                "acquisition_start", "acquisition_end", "parameters",
                "notes", "metadata", "created_at",
            ],
        ),
        "processing": _Table(
            columns=[
                "id", "data_asset_id", "processing_pipeline", "version",
                "parameters", "notes", "started_at", "completed_at",
                "metadata", "created_at",
            ],
        ),
        "quality_control": _Table(
            columns=[
                "id", "data_asset_id", "qc_metric", "value", "unit",
                "status", "notes", "metadata", "created_at",
            ],
        ),
        "data_description": _Table(
            columns=[
                "id", "data_asset_id", "description_kind", "text",
                "language", "funding_source", "license", "metadata",
                "created_at",
            ],
        ),
        "data_asset_subject": _Table(
            columns=["data_asset_id", "subject_id", "linked_at"],
            composite_pk_cols=("data_asset_id", "subject_id"),
        ),
        "data_asset_instrument": _Table(
            columns=["data_asset_id", "instrument_id", "linked_at"],
            composite_pk_cols=("data_asset_id", "instrument_id"),
        ),
        "data_asset_rig": _Table(
            columns=["data_asset_id", "rig_id", "linked_at"],
            composite_pk_cols=("data_asset_id", "rig_id"),
        ),
        "data_asset_procedures": _Table(
            columns=["data_asset_id", "procedures_id", "linked_at"],
            composite_pk_cols=("data_asset_id", "procedures_id"),
        ),
    }


class _FakeCursor:
    """Cursor that interprets the seeder's INSERT / UPSERT / SELECT shapes."""

    def __init__(self, conn: "FakeConn") -> None:
        self._conn = conn
        self._fetch_buffer: List[Tuple[Any, ...]] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False

    def execute(
        self, sql: str, params: Optional[Tuple[Any, ...]] = None
    ) -> None:
        params = params or ()
        self._conn.executed_statements.append((sql, params))
        normalized = " ".join(sql.split())

        # --- WITH upserted AS (...) SELECT id, inserted FROM upserted -----
        m = _UPSERT_CTE_RE.match(normalized)
        if m is not None:
            self._handle_upsert_cte(
                table=m.group("table"),
                cols=[c.strip() for c in m.group("cols").split(",")],
                conflict_col=m.group("conflict"),
                params=params,
            )
            return

        # --- INSERT ... ON CONFLICT (col) DO NOTHING RETURNING id ----------
        # --- INSERT ... ON CONFLICT DO NOTHING (no target — junctions) ----
        # --- INSERT ... RETURNING id (no conflict) -------------------------
        m = _INSERT_RE.match(normalized)
        if m is not None:
            table = m.group("table")
            cols = [c.strip() for c in m.group("cols").split(",")]
            rest = m.group("rest")
            target_match = _ON_CONFLICT_TARGET_RE.search(rest)
            do_nothing = bool(_ON_CONFLICT_DO_NOTHING_RE.search(rest))
            do_update = bool(_ON_CONFLICT_DO_UPDATE_RE.search(rest))
            returning = bool(_RETURNING_RE.search(rest))

            self._handle_insert(
                table=table,
                cols=cols,
                params=params,
                conflict_col=(
                    target_match.group("col").strip()
                    if target_match
                    else None
                ),
                do_nothing=do_nothing,
                do_update=do_update,
                returning=returning,
            )
            return

        # --- SELECT id FROM <table> WHERE <col> = %s -----------------------
        m = _SELECT_RE.match(normalized)
        if m is not None:
            table = m.group("table")
            col = m.group("col")
            value = params[0] if params else None
            row = self._conn.find_row(table, col, value)
            self._fetch_buffer = [(row["id"],)] if row is not None else []
            return

        raise AssertionError(
            f"FakeConn does not understand SQL: {sql.strip()!r} (params={params!r})"
        )

    # ---- INSERT shapes ---------------------------------------------------

    def _handle_insert(
        self,
        *,
        table: str,
        cols: List[str],
        params: Tuple[Any, ...],
        conflict_col: Optional[str],
        do_nothing: bool,
        do_update: bool,
        returning: bool,
    ) -> None:
        row = dict(zip(cols, params))
        existing = self._conn.find_conflict_row(table, row, conflict_col)

        if existing is not None:
            # ON CONFLICT DO NOTHING -> RETURNING returns no row.
            if do_nothing:
                self._fetch_buffer = []
                return
            if do_update and returning:
                # Plain ``INSERT ... ON CONFLICT (col) DO UPDATE SET
                # ... RETURNING id`` is what the bootstrap uses (it
                # doesn't need the ``inserted`` boolean — it just
                # wants the existing/new id). Return the existing
                # row's id.
                self._fetch_buffer = [(existing["id"],)]
                return
            self._fetch_buffer = []
            return

        new_row = self._conn.insert(table, row)
        if returning:
            self._fetch_buffer = [(new_row["id"],)]
        else:
            self._fetch_buffer = []

    def _handle_upsert_cte(
        self,
        *,
        table: str,
        cols: List[str],
        conflict_col: str,
        params: Tuple[Any, ...],
    ) -> None:
        row = dict(zip(cols, params))
        existing = self._conn.find_conflict_row(table, row, conflict_col)
        if existing is not None:
            # Touching the conflict column to itself is a no-op update;
            # ``inserted`` is False (xmax != 0 in real Postgres).
            self._fetch_buffer = [(existing["id"], False)]
            return
        new_row = self._conn.insert(table, row)
        self._fetch_buffer = [(new_row["id"], True)]

    def fetchone(self) -> Optional[Tuple[Any, ...]]:
        return self._fetch_buffer.pop(0) if self._fetch_buffer else None

    def fetchall(self) -> List[Tuple[Any, ...]]:
        out, self._fetch_buffer = self._fetch_buffer, []
        return out


class FakeConn:
    """In-memory Postgres-shaped connection for seeder tests."""

    def __init__(self) -> None:
        self.tables: Dict[str, _Table] = _build_fake_schema()
        self.executed_statements: List[Tuple[str, Tuple[Any, ...]]] = []
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

    # ---- Table helpers ---------------------------------------------------

    def find_conflict_row(
        self,
        table: str,
        row: Dict[str, Any],
        conflict_col: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """Find an existing row that violates a UNIQUE / PK constraint."""
        t = self.tables[table]
        if conflict_col is not None:
            return self.find_row(table, conflict_col, row.get(conflict_col))

        # Composite PK (junction tables): match all PK columns.
        if t.composite_pk_cols is not None:
            for existing in t.rows:
                if all(
                    existing.get(c) == row.get(c) for c in t.composite_pk_cols
                ):
                    return existing
            return None

        # No conflict target and no composite PK — no conflict possible.
        return None

    def find_row(
        self, table: str, col: str, value: Any
    ) -> Optional[Dict[str, Any]]:
        # Special-case the composite UNIQUE on space (org_id, name).
        if table == "space" and col in ("name", "__org_name__"):
            return None  # the seeder uses the composite form below
        for r in self.tables[table].rows:
            if r.get(col) == value:
                return r
        return None

    def insert(self, table: str, row: Dict[str, Any]) -> Dict[str, Any]:
        t = self.tables[table]
        # Check composite UNIQUE (space.org_id, name) before inserting.
        if table == "space":
            for r in t.rows:
                if (
                    r.get("org_id") == row.get("org_id")
                    and r.get("name") == row.get("name")
                ):
                    return r
        new_row = dict(row)
        if "id" not in new_row or new_row.get("id") is None:
            # Use deterministic UUID-shaped strings so the test
            # output is greppable and stable across runs.
            new_row["id"] = f"{table}-id-{t.next_id:04d}"
            t.next_id += 1
        t.rows.append(new_row)
        return new_row

    # ---- Test helpers ----------------------------------------------------

    def count(self, table: str) -> int:
        return len(self.tables[table].rows)


# ---------------------------------------------------------------------------
# FakeS3Client.
# ---------------------------------------------------------------------------


class FakeS3Client:
    """Returns a fixed bytes payload from ``get_object``."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.calls: List[Dict[str, Any]] = []

    def get_object(self, *, Bucket: str, Key: str) -> Dict[str, Any]:
        self.calls.append({"Bucket": Bucket, "Key": Key})
        return {"Body": io.BytesIO(self._payload)}


# ---------------------------------------------------------------------------
# Helpers / fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture()
def fixture_payload() -> bytes:
    return _FIXTURE_PATH.read_bytes()


@pytest.fixture()
def fake_s3(fixture_payload: bytes) -> FakeS3Client:
    return FakeS3Client(fixture_payload)


@pytest.fixture()
def fake_conn() -> FakeConn:
    return FakeConn()


# ---------------------------------------------------------------------------
# 1) Mapping logic produces correct INSERTs for the 5-record fixture.
# ---------------------------------------------------------------------------


def test_run_seeder_inserts_all_records_at_fraction_one(
    fake_conn: FakeConn, fake_s3: FakeS3Client
) -> None:
    summary = seeder.run_seeder(
        conn=fake_conn,
        s3_client=fake_s3,
        bucket="aind-scratch-data",
        key="jon.young/metadata_v2_records_20260324/data_assets.json",
        sample_fraction=1.0,
    )

    # All 5 records seen + sampled.
    assert summary.records_seen == 5
    assert summary.records_sampled == 5
    assert summary.records_skipped_unmappable == 0
    assert summary.errors == []

    # All 5 Data_Assets land.
    assert summary.data_assets_inserted == 5
    assert summary.data_assets_skipped == 0
    assert fake_conn.count("data_asset") == 5

    # Bootstrap: 1 system org / 1 system user / 1 default space.
    assert fake_conn.count("organization") == 1
    assert fake_conn.count("app_user") == 1
    assert fake_conn.count("space") == 1
    org = fake_conn.tables["organization"].rows[0]
    user = fake_conn.tables["app_user"].rows[0]
    space = fake_conn.tables["space"].rows[0]
    assert org["name"] == seeder.SYSTEM_ORG_NAME
    assert user["cognito_sub"] == seeder.SYSTEM_USER_COGNITO_SUB
    assert space["name"] == seeder.DEFAULT_SPACE_NAME

    # Two records share subject_id="695464"; the seeder must dedupe via
    # the UNIQUE constraint and reuse the row.
    assert summary.subjects_inserted == 3  # 695464, 700000, 900001
    assert summary.subjects_reused == 1    # second exaSPIM record reuses 695464
    assert fake_conn.count("subject") == 3

    # 4 distinct instruments across 5 records (the minimal record has
    # none).
    assert summary.instruments_inserted == 4
    assert fake_conn.count("instrument") == 4

    # 2 distinct rigs across the records that carry one (exaSPIM-rig-1,
    # mesoscope-rig-A — the ephys/minimal/fMOST records have no rig).
    assert summary.rigs_inserted == 2
    assert fake_conn.count("rig") == 2

    # 1 procedures row (only the first record carries Procedures).
    assert summary.procedures_inserted == 1
    assert fake_conn.count("procedures") == 1

    # 3 records carry a session (exaSPIM, ophys, ephys); the minimal
    # and fMOST records do not.
    assert summary.sessions_inserted == 3
    assert fake_conn.count("session") == 3

    # 2 records carry an acquisition.
    assert summary.acquisitions_inserted == 2

    # 1 record carries processing.
    assert summary.processings_inserted == 1

    # 2 records carry QC.
    assert summary.quality_controls_inserted == 2

    # 3 records carry data_description (exaSPIM, ophys, fMOST). The
    # ephys and minimal records do not.
    assert summary.data_descriptions_inserted == 3


def test_run_seeder_creates_junction_rows(
    fake_conn: FakeConn, fake_s3: FakeS3Client
) -> None:
    seeder.run_seeder(
        conn=fake_conn,
        s3_client=fake_s3,
        bucket="b",
        key="k",
        sample_fraction=1.0,
    )

    # 4 records with subjects -> 4 data_asset_subject rows.
    assert fake_conn.count("data_asset_subject") == 4
    # 4 records with instruments.
    assert fake_conn.count("data_asset_instrument") == 4
    # 2 records with rigs.
    assert fake_conn.count("data_asset_rig") == 2
    # 1 record with procedures.
    assert fake_conn.count("data_asset_procedures") == 1


def test_run_seeder_attributes_rows_to_system_user_and_space(
    fake_conn: FakeConn, fake_s3: FakeS3Client
) -> None:
    seeder.run_seeder(
        conn=fake_conn,
        s3_client=fake_s3,
        bucket="b",
        key="k",
        sample_fraction=1.0,
    )

    user = fake_conn.tables["app_user"].rows[0]
    space = fake_conn.tables["space"].rows[0]
    assert all(
        a["created_by"] == user["id"]
        for a in fake_conn.tables["data_asset"].rows
    )
    assert all(
        a["space_id"] == space["id"]
        for a in fake_conn.tables["data_asset"].rows
    )


def test_run_seeder_records_correct_storage_uris(
    fake_conn: FakeConn, fake_s3: FakeS3Client
) -> None:
    seeder.run_seeder(
        conn=fake_conn,
        s3_client=fake_s3,
        bucket="b",
        key="k",
        sample_fraction=1.0,
    )

    uris = sorted(a["storage_uri"] for a in fake_conn.tables["data_asset"].rows)
    assert uris == sorted([
        "s3://aind-open-data/exaSPIM_695464_2024-09-12_18-03-29",
        "s3://aind-open-data/ophys_695464_2024-08-01_10-22-00",
        "s3://aind-open-data/ecephys_700000_2024-10-05_09-00-00",
        "s3://aind-open-data/minimal_record_2024-11-01",
        "s3://aind-open-data/fmost_900001_2024-11-30_07-00-00",
    ])


def test_run_seeder_persists_metadata_blob_with_source_record(
    fake_conn: FakeConn, fake_s3: FakeS3Client
) -> None:
    seeder.run_seeder(
        conn=fake_conn,
        s3_client=fake_s3,
        bucket="b",
        key="k",
        sample_fraction=1.0,
    )

    for a in fake_conn.tables["data_asset"].rows:
        # pg8000 columns coming through our seeder are JSON-encoded
        # strings (see seeder._jsonable_param). Decode and inspect.
        md = json.loads(a["metadata"])
        assert "source_record" in md
        assert "__seeder" in md
        assert "content_hash" in md["__seeder"]


def test_run_seeder_commits_per_record_plus_bootstrap(
    fake_conn: FakeConn, fake_s3: FakeS3Client
) -> None:
    seeder.run_seeder(
        conn=fake_conn,
        s3_client=fake_s3,
        bucket="b",
        key="k",
        sample_fraction=1.0,
    )
    # 1 commit for the bootstrap + 1 commit per inserted record.
    # 5 records all freshly inserted -> 6 commits total.
    assert fake_conn.commit_count == 6


# ---------------------------------------------------------------------------
# 2) Idempotency — re-run produces zero new INSERTs.
# ---------------------------------------------------------------------------


def test_run_seeder_is_idempotent_on_rerun(
    fake_conn: FakeConn, fake_s3: FakeS3Client
) -> None:
    """Re-running the seeder against the same DB state and same fixture
    must produce zero new INSERTs.

    Validates: R32.5 (idempotent ``terraform apply``).
    """
    s1 = seeder.run_seeder(
        conn=fake_conn,
        s3_client=fake_s3,
        bucket="b",
        key="k",
        sample_fraction=1.0,
    )

    # Snapshot row counts after the first run.
    before = {
        name: fake_conn.count(name)
        for name in fake_conn.tables.keys()
    }

    # Reset the second-pass S3 client (BytesIO is consumed once).
    fake_s3._payload = _FIXTURE_PATH.read_bytes()  # noqa: SLF001 — test only

    s2 = seeder.run_seeder(
        conn=fake_conn,
        s3_client=fake_s3,
        bucket="b",
        key="k",
        sample_fraction=1.0,
    )

    # Same number of records seen + sampled.
    assert s2.records_seen == s1.records_seen
    assert s2.records_sampled == s1.records_sampled

    # No new asset/subject/instrument/rig inserts. Note: with the
    # reorder-for-idempotency in seeder._insert_mapped_record (assets
    # checked FIRST, short-circuit on conflict), the seeder never even
    # attempts the shared-entity upsert on a re-run — so the
    # ``*_reused`` counters stay at zero. The "shared entities are not
    # touched on a re-run" property is exactly what we want.
    assert s2.data_assets_inserted == 0
    assert s2.data_assets_skipped == 5
    assert s2.subjects_inserted == 0
    assert s2.subjects_reused == 0
    assert s2.instruments_inserted == 0
    assert s2.instruments_reused == 0
    assert s2.rigs_inserted == 0
    assert s2.rigs_reused == 0

    # No new asset-specific entities (asset existed → child rows
    # short-circuited).
    assert s2.procedures_inserted == 0
    assert s2.sessions_inserted == 0
    assert s2.acquisitions_inserted == 0
    assert s2.processings_inserted == 0
    assert s2.quality_controls_inserted == 0
    assert s2.data_descriptions_inserted == 0
    assert s2.junctions_inserted == 0

    # Row counts are unchanged.
    after = {
        name: fake_conn.count(name)
        for name in fake_conn.tables.keys()
    }
    assert before == after


# ---------------------------------------------------------------------------
# 3) Sampling determinism — same hash modulo gives same subset.
# ---------------------------------------------------------------------------


def test_sampling_subset_is_deterministic_at_fraction_below_one(
    fixture_payload: bytes,
) -> None:
    """Run the seeder twice at the same fraction (<1) on a fresh DB
    each time and assert the same Data_Asset URIs land both times."""
    conn1 = FakeConn()
    conn2 = FakeConn()
    s3a = FakeS3Client(fixture_payload)
    s3b = FakeS3Client(fixture_payload)

    fraction = 0.4

    seeder.run_seeder(
        conn=conn1, s3_client=s3a, bucket="b", key="k",
        sample_fraction=fraction,
    )
    seeder.run_seeder(
        conn=conn2, s3_client=s3b, bucket="b", key="k",
        sample_fraction=fraction,
    )

    uris1 = sorted(a["storage_uri"] for a in conn1.tables["data_asset"].rows)
    uris2 = sorted(a["storage_uri"] for a in conn2.tables["data_asset"].rows)
    assert uris1 == uris2


def test_sampling_at_zero_fraction_inserts_no_records(
    fixture_payload: bytes,
) -> None:
    conn = FakeConn()
    fake_s3 = FakeS3Client(fixture_payload)
    with pytest.raises(seeder.SeederError):
        seeder.run_seeder(
            conn=conn, s3_client=fake_s3, bucket="b", key="k",
            sample_fraction=0.0,
        )


def test_sampling_size_is_within_expected_bounds(
    fixture_payload: bytes,
) -> None:
    """Across 200 deterministic record-shaped seeds, ~10% sampling
    selects between 5% and 20% of records (statistical bound).

    This is the seeder-level analogue of the should_sample
    distribution test in test_mapping.py.
    """
    # Build a synthetic 200-record corpus with stable storage URIs.
    records = [
        {
            "name": f"rec-{i}",
            "storage_uri": f"s3://test/asset/{i:04d}",
        }
        for i in range(200)
    ]
    payload = json.dumps(records).encode("utf-8")
    conn = FakeConn()
    fake_s3 = FakeS3Client(payload)

    summary = seeder.run_seeder(
        conn=conn, s3_client=fake_s3, bucket="b", key="k",
        sample_fraction=0.1,
    )
    # Hash-based sampling at 0.1 over 200 records: ~20 expected,
    # 99.9% binomial CI [9, 32]; we use a wide bound for portability.
    assert 5 <= summary.records_sampled <= 40


# ---------------------------------------------------------------------------
# 4) Missing optional fields don't crash.
# ---------------------------------------------------------------------------


def test_minimal_record_lands_with_no_shared_entities_no_children(
    fake_conn: FakeConn, fake_s3: FakeS3Client
) -> None:
    seeder.run_seeder(
        conn=fake_conn, s3_client=fake_s3, bucket="b", key="k",
        sample_fraction=1.0,
    )
    minimal = next(
        a for a in fake_conn.tables["data_asset"].rows
        if "minimal_record" in a["storage_uri"]
    )
    asset_id = minimal["id"]

    # No junctions for this asset.
    for jt in (
        "data_asset_subject",
        "data_asset_instrument",
        "data_asset_rig",
        "data_asset_procedures",
    ):
        assert all(
            r["data_asset_id"] != asset_id for r in fake_conn.tables[jt].rows
        )
    # No asset-specific children.
    for child in (
        "session", "acquisition", "processing", "quality_control",
        "data_description",
    ):
        assert all(
            r["data_asset_id"] != asset_id for r in fake_conn.tables[child].rows
        )


# ---------------------------------------------------------------------------
# Source-shape robustness: NDJSON.
# ---------------------------------------------------------------------------


def test_run_seeder_accepts_ndjson_payload() -> None:
    records = [
        {"storage_uri": f"s3://test/a/{i}", "name": f"r{i}"} for i in range(3)
    ]
    payload = "\n".join(json.dumps(r) for r in records).encode("utf-8")
    conn = FakeConn()
    s3 = FakeS3Client(payload)

    summary = seeder.run_seeder(
        conn=conn, s3_client=s3, bucket="b", key="k",
        sample_fraction=1.0,
    )
    assert summary.records_seen == 3
    assert summary.data_assets_inserted == 3


def test_run_seeder_rejects_unrecognised_payload() -> None:
    conn = FakeConn()
    s3 = FakeS3Client(b"not-json-at-all")
    with pytest.raises(seeder.SeederError):
        seeder.run_seeder(
            conn=conn, s3_client=s3, bucket="b", key="k",
            sample_fraction=1.0,
        )


# ---------------------------------------------------------------------------
# Bootstrap idempotency.
# ---------------------------------------------------------------------------


def test_bootstrap_returns_existing_ids_on_second_call() -> None:
    conn = FakeConn()
    first = seeder.bootstrap_system_principal(conn)
    second = seeder.bootstrap_system_principal(conn)
    assert first.system_user_id == second.system_user_id
    assert first.system_org_id == second.system_org_id
    assert first.default_space_id == second.default_space_id
    # Still exactly one row each.
    assert conn.count("organization") == 1
    assert conn.count("app_user") == 1
    assert conn.count("space") == 1


# ---------------------------------------------------------------------------
# max_records cap.
# ---------------------------------------------------------------------------


def test_max_records_caps_processing(
    fake_conn: FakeConn, fake_s3: FakeS3Client
) -> None:
    summary = seeder.run_seeder(
        conn=fake_conn, s3_client=fake_s3, bucket="b", key="k",
        sample_fraction=1.0,
        max_records=2,
    )
    assert summary.records_seen == 2
    assert summary.data_assets_inserted == 2
    assert fake_conn.count("data_asset") == 2

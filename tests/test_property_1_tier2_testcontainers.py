"""
Feature: allen-biodata-registry-poc, Property 1 (Tier 2):
RLS Universal Visibility — testcontainers Postgres
Task: 29.3

Tier 2 integration PBT: spins up a real Postgres in a Docker container
via `testcontainers`, applies our migrations 0001-0007 (the schema +
governance + RLS policies), then runs Hypothesis-generated assertions
that the database's `SELECT ... FROM data_asset` results agree with
the Tier 1 `compute_visibility` reference for the same inputs.

This is the slow tier — it requires Docker and is intended to run
nightly in CI, not on every commit. When Docker is unavailable the
suite skips (so local `pytest` doesn't fail just because the dev
laptop hasn't installed Docker).

Validates: R10.1, R10.2, R10.3 | Design: §Correctness Properties.Property 1,
§Testing Strategy.Two-tier PBT (Tier 2).
"""
from __future__ import annotations

import os
import re
import sys
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

# Skip entirely when testcontainers / Docker unavailable.
testcontainers = pytest.importorskip(
    "testcontainers.postgres",
    reason="Tier 2 PBT requires testcontainers + Docker; skipped in local fast loop.",
)
from testcontainers.postgres import PostgresContainer  # noqa: E402

try:
    import psycopg  # type: ignore[import-untyped]
except ImportError:
    pytest.skip("psycopg required for Tier 2 PBT", allow_module_level=True)

from hypothesis import HealthCheck, given, settings, strategies as st  # noqa: E402

# Bring the Tier 1 reference into scope so we can compare DB output
# against the same oracle.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_property_1_rls_visibility import compute_visibility  # noqa: E402


# ---------------------------------------------------------------------------
# Migrations to apply against the testcontainer.
#
# Migrations 0001-0007 contain the schema + RLS policies. We strip out
# pgvector and the embedding columns since the official `postgres:16`
# image doesn't ship pgvector — Aurora has it via parameter group, but
# testcontainers Postgres needs the `pgvector/pgvector:pg16` image.
# We use `pgvector/pgvector:pg16` precisely so we don't need to edit
# the migrations.
# ---------------------------------------------------------------------------

_PG_IMAGE = "pgvector/pgvector:pg16"
_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_TIER2_MIGRATIONS = [
    "0001_governance.sql",
    "0002_data_asset.sql",
    "0003_junctions.sql",
    "0004_revisions_lifecycle_duplicates.sql",
    "0005_collections_schemas.sql",
    "0006_rls_policies.sql",
    "0007_search_indexes.sql",
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def pg_container():
    """Boot a Postgres container with pgvector enabled and apply the
    registry migrations. Module-scoped because container startup is
    expensive (~10s) and the schema is read-only across tests."""
    try:
        container = PostgresContainer(image=_PG_IMAGE)
        container.start()
    except Exception as exc:
        pytest.skip(f"unable to start testcontainer ({exc!s}); Docker not available")

    yield container
    container.stop()


@pytest.fixture(scope="module")
def pg_dsn(pg_container):
    """psycopg connection URL for the containerized database."""
    return pg_container.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")


@pytest.fixture(scope="module")
def applied_schema(pg_dsn):
    """Apply migrations + create a `biodata_app` role, return None.
    Module-scoped so the full migration set runs once per session."""
    conn = psycopg.connect(pg_dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            # Some migrations reference the biodata_app role for grants;
            # create it up-front so they don't error.
            cur.execute("CREATE ROLE biodata_app NOLOGIN")
        for fname in _TIER2_MIGRATIONS:
            sql_path = _MIGRATIONS_DIR / fname
            sql = sql_path.read_text(encoding="utf-8")
            with conn.cursor() as cur:
                cur.execute(sql)
        # Set up an `app_user` row whose UUID we'll use as the RLS
        # principal for queries (not strictly needed for the policies
        # but referenced in some FK constraints).
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app_user (cognito_sub, email) "
                "VALUES ('tier2-system', 'system@tier2.local') "
                "RETURNING id"
            )
            (system_user_id,) = cur.fetchone()
    finally:
        conn.close()
    return {"system_user_id": str(system_user_id)}


@pytest.fixture
def conn(pg_dsn, applied_schema):
    c = psycopg.connect(pg_dsn)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_rls_context(cur, user_id: str, org_ids: List[str], space_ids: List[str], roles: List[str]):
    """Mirror the production helper — string-quoted SET LOCAL because
    Postgres rejects parameter placeholders in SET statements."""
    def _q(s: str) -> str:
        return "'" + s.replace("'", "''") + "'"
    cur.execute(f"SET LOCAL app.current_user_id = {_q(user_id)}")
    cur.execute(f"SET LOCAL app.current_org_ids = {_q(','.join(org_ids))}")
    cur.execute(f"SET LOCAL app.current_space_ids = {_q(','.join(space_ids))}")
    cur.execute(f"SET LOCAL app.current_roles = {_q(','.join(roles))}")


def _seed_org_space(conn) -> Tuple[str, str, str]:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organization (name, display_name) "
            "VALUES (%s, %s) RETURNING id",
            (f"org-{uuid.uuid4().hex[:6]}", "tier2"),
        )
        (org_id,) = cur.fetchone()
        cur.execute(
            "INSERT INTO space (org_id, name, display_name) "
            "VALUES (%s, %s, %s) RETURNING id",
            (org_id, f"sp-{uuid.uuid4().hex[:6]}", "tier2"),
        )
        (space_id,) = cur.fetchone()
        cur.execute(
            "INSERT INTO app_user (cognito_sub, email) "
            "VALUES (%s, %s) RETURNING id",
            (f"u-{uuid.uuid4().hex[:6]}", f"u-{uuid.uuid4().hex[:6]}@tier2.local"),
        )
        (user_id,) = cur.fetchone()
    conn.commit()
    return str(org_id), str(space_id), str(user_id)


# ---------------------------------------------------------------------------
# Hypothesis strategies (mirroring Tier 1)
# ---------------------------------------------------------------------------

_LIFECYCLE = st.sampled_from(["draft", "registered", "published", "archived"])
_VALIDATION = st.sampled_from(["unvalidated", "valid", "invalid", "schema-deprecated"])
_ROLE = st.sampled_from([
    "viewer", "contributor", "space_admin",
    "org_admin", "data_administrator", "system",
])


@st.composite
def _scenario(draw):
    return {
        "lifecycle_state":   draw(_LIFECYCLE),
        "validation_status": draw(_VALIDATION),
        "sensitive_flag":    draw(st.booleans()),
        "user_in_space":     draw(st.booleans()),
        "user_in_org":       draw(st.booleans()),
        "user_roles":        draw(st.lists(_ROLE, min_size=1, max_size=3, unique=True)),
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
@given(_scenario())
def test_db_visibility_matches_compute_visibility(conn, applied_schema, scenario):
    """The asset is visible via SELECT FROM data_asset iff
    compute_visibility() says so. We seed an org + space, INSERT one
    asset with the scenario's properties, then run SELECT under
    different RLS contexts."""
    org_id, space_id, owner_id = _seed_org_space(conn)
    asset_id = uuid.uuid4()

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO data_asset
                (id, name, storage_uri, data_type,
                 org_id, space_id, lifecycle_state, validation_status,
                 sensitive_flag, created_by, updated_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                asset_id,
                f"a-{asset_id.hex[:6]}",
                f"s3://b/{asset_id.hex}",
                "behavior",
                org_id, space_id,
                scenario["lifecycle_state"], scenario["validation_status"],
                scenario["sensitive_flag"], owner_id, owner_id,
            ),
        )
    conn.commit()

    user_org_ids   = [org_id] if scenario["user_in_org"] else []
    user_space_ids = [space_id] if scenario["user_in_space"] else []

    expected = compute_visibility(
        user_context={
            "user_id": owner_id,
            "roles": scenario["user_roles"],
            "org_ids": user_org_ids,
            "space_ids": user_space_ids,
        },
        asset={
            "space_id": space_id,
            "org_id": org_id,
            "lifecycle_state": scenario["lifecycle_state"],
            "validation_status": scenario["validation_status"],
            "sensitive_flag": scenario["sensitive_flag"],
        },
        sharing_grants=[],
    )

    # Run as a non-superuser (RLS only applies when role is not BYPASSRLS).
    with conn.cursor() as cur:
        cur.execute("SET LOCAL ROLE biodata_app")
        _set_rls_context(
            cur, owner_id, user_org_ids, user_space_ids, scenario["user_roles"]
        )
        cur.execute("SELECT id FROM data_asset WHERE id = %s", (asset_id,))
        rows = cur.fetchall()
    actual = bool(rows)
    conn.rollback()  # release locks; SET LOCAL only persists in tx anyway

    assert actual == expected, (
        f"DB and compute_visibility disagreed: "
        f"db={actual} oracle={expected} scenario={scenario}"
    )


def test_smoke_published_valid_visible_to_anonymous(conn, applied_schema):
    """Smoke test: a published+valid asset is visible to a user with no
    org/space/role membership."""
    org_id, space_id, owner_id = _seed_org_space(conn)
    asset_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO data_asset
                (id, name, storage_uri, data_type,
                 org_id, space_id, lifecycle_state, validation_status,
                 sensitive_flag, created_by, updated_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                asset_id, "smoke-pub", f"s3://b/{asset_id.hex}", "behavior",
                org_id, space_id, "published", "valid", False, owner_id, owner_id,
            ),
        )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SET LOCAL ROLE biodata_app")
        _set_rls_context(cur, owner_id, [], [], [])
        cur.execute("SELECT id FROM data_asset WHERE id = %s", (asset_id,))
        rows = cur.fetchall()
    assert rows
    conn.rollback()


def test_smoke_sensitive_blocks_non_privileged(conn, applied_schema):
    """Smoke: a sensitive published-valid asset is hidden from a viewer
    in the owning space."""
    org_id, space_id, owner_id = _seed_org_space(conn)
    asset_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO data_asset
                (id, name, storage_uri, data_type,
                 org_id, space_id, lifecycle_state, validation_status,
                 sensitive_flag, created_by, updated_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                asset_id, "smoke-sens", f"s3://b/{asset_id.hex}", "behavior",
                org_id, space_id, "published", "valid", True, owner_id, owner_id,
            ),
        )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SET LOCAL ROLE biodata_app")
        _set_rls_context(cur, owner_id, [org_id], [space_id], ["viewer"])
        cur.execute("SELECT id FROM data_asset WHERE id = %s", (asset_id,))
        rows = cur.fetchall()
    assert not rows
    conn.rollback()


def test_smoke_data_admin_sees_sensitive(conn, applied_schema):
    """Smoke: data_administrator pierces sensitive_flag."""
    org_id, space_id, owner_id = _seed_org_space(conn)
    asset_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO data_asset
                (id, name, storage_uri, data_type,
                 org_id, space_id, lifecycle_state, validation_status,
                 sensitive_flag, created_by, updated_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                asset_id, "smoke-admin", f"s3://b/{asset_id.hex}", "behavior",
                org_id, space_id, "draft", "unvalidated", True, owner_id, owner_id,
            ),
        )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SET LOCAL ROLE biodata_app")
        _set_rls_context(cur, owner_id, [org_id], [], ["data_administrator"])
        cur.execute("SELECT id FROM data_asset WHERE id = %s", (asset_id,))
        rows = cur.fetchall()
    assert rows
    conn.rollback()

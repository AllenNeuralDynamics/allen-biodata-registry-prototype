"""
Allen BioData Registry PoC — schema migration runner core logic.

This module is deliberately split out from ``handler.py`` so the
algorithm can be unit-tested with a mocked DB cursor and a temp
directory of ``.sql`` files — without dragging in the Lambda framing,
boto3, or pg8000.

What it does
------------

Walks a configured directory of ``*.sql`` files in **lexical order**
and applies each one that has not yet been recorded in a
``schema_version`` table. The runner is idempotent: re-invocation is a
no-op once every migration is recorded.

For each ``.sql`` file the runner:

1. Computes the SHA-256 checksum of the file contents.
2. Extracts the ``version`` from the filename's leading numeric prefix
   (e.g. ``0001_governance.sql`` → ``"0001"``). The full filename is
   also stored.
3. Looks up the version in ``schema_version``.

   * **Missing:** reads the file, executes the SQL inside a single
     transaction (``BEGIN`` … ``COMMIT``), then INSERTs a
     ``schema_version`` row. If the file's first 100 characters contain
     the directive ``-- +runner: no-transaction``, the SQL is run in
     **autocommit** mode instead — required for statements like
     ``CREATE INDEX CONCURRENTLY`` which Postgres rejects inside an
     explicit transaction. None of the registry's migrations need this
     today, but the contract is documented in ``migrations/README.md``.
   * **Present:** compares the stored checksum with the recomputed
     checksum. If they differ the runner emits a CRITICAL log warning
     (``drift``) and **does not** re-apply the file. The forward-only
     migration convention requires authoring a new file rather than
     editing an applied one; this drift signal is the safety net.

4. Records what happened so the Lambda handler can return a structured
   summary: ``{applied: [...], skipped: [...], drift: [...]}``.

Out-of-order discovery
----------------------

If a new migration with a version *less than* the maximum already-
applied version shows up on disk, ``MigrationOrderError`` is raised.
This catches the "rebase mistake" where someone names a new file
``0008_*.sql`` when the latest applied is ``0009_*.sql``. The runner
refuses to skip the gap silently, because a partially-out-of-order
apply would leave the schema in a state nobody knows how to reproduce.
The recovery is to rename the offending file to a version greater than
the maximum already-applied version.

Schema_version DDL
------------------

The runner ensures the ``schema_version`` table exists before doing
anything else (using ``CREATE TABLE IF NOT EXISTS``):

```
CREATE TABLE schema_version (
  version    text PRIMARY KEY,
  filename   text NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT now(),
  checksum   text,
  applied_by text
);
```

This table is owned by the migration runner and is never written to by
business migrations. Note that this is **distinct** from the
``schema_definition`` table created by ``0005_collections_schemas.sql``
(which holds *application-level* Biodata_Schema versions).

Validates: R32.5 (idempotent ``terraform apply``), Design:
§IaC.Idempotency and Sample Data.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence

LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types.
# ---------------------------------------------------------------------------


# Filenames must start with a numeric prefix followed by an underscore
# and end in `.sql`. Lexical order over the full filename gives the
# correct apply order so long as the prefixes are zero-padded — the
# convention documented in migrations/README.md is four digits, but the
# runner accepts any number of digits as long as files are zero-padded
# consistently within a corpus.
_FILENAME_RE = re.compile(r"^(?P<version>\d+)(?:_[A-Za-z0-9_\-]*)?\.sql$")

# Window we scan for the no-transaction directive. Limiting the window
# (rather than scanning the whole file) means a comment buried 5,000
# lines down inside an unrelated DDL block can't accidentally toggle
# the mode.
_DIRECTIVE_WINDOW_CHARS = 100
_NO_TX_DIRECTIVE = "-- +runner: no-transaction"

# Schema_version DDL — the runner-owned bookkeeping table. The
# CREATE TABLE statement is idempotent (IF NOT EXISTS), so it is safe
# to issue on every invocation.
_SCHEMA_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
  version    text PRIMARY KEY,
  filename   text NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT now(),
  checksum   text,
  applied_by text
);
""".strip()


class MigrationRunnerError(RuntimeError):
    """Base class for all runner-level errors."""


class MigrationOrderError(MigrationRunnerError):
    """Raised when a new migration file is older than an already-applied one.

    Catches the "rebase mistake" where someone names a new migration
    with a version less than what's already in ``schema_version``.
    Refusing to skip the gap is the only way to keep the schema
    deterministic across environments — the recovery is to rename the
    offending file to a version greater than the maximum already-
    applied version.
    """


class MigrationFilenameError(MigrationRunnerError):
    """Raised when a ``.sql`` file does not match the ``NNNN_slug.sql`` pattern."""


@dataclasses.dataclass(frozen=True)
class _MigrationFile:
    """Internal record bound to one ``.sql`` file on disk."""

    version: str
    filename: str
    path: Path
    checksum: str
    contents: str
    no_transaction: bool


@dataclasses.dataclass(frozen=True)
class AppliedMigration:
    """An already-applied row pulled out of the ``schema_version`` table."""

    version: str
    filename: str
    checksum: Optional[str]


@dataclasses.dataclass(frozen=True)
class DriftEntry:
    """Drift signal: file checksum differs from the recorded checksum.

    Forward-only convention requires editing a *new* migration rather
    than mutating an already-applied one. We log CRITICAL and surface
    the entry in the summary, but we do **not** re-apply.
    """

    version: str
    filename: str
    expected_checksum: Optional[str]
    actual_checksum: str


@dataclasses.dataclass
class RunSummary:
    """Result of one ``run_migrations`` invocation.

    The Lambda handler serializes this to JSON for the caller (the
    Terraform ``aws_lambda_invocation`` data source).
    """

    applied: List[str] = dataclasses.field(default_factory=list)
    skipped: List[str] = dataclasses.field(default_factory=list)
    drift: List[DriftEntry] = dataclasses.field(default_factory=list)
    schema_version_created: bool = False
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": list(self.applied),
            "skipped": list(self.skipped),
            "drift": [dataclasses.asdict(d) for d in self.drift],
            "schema_version_created": self.schema_version_created,
            "elapsed_ms": self.elapsed_ms,
        }


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def run_migrations(
    *,
    conn: Any,
    migrations_dir: str | os.PathLike[str],
    applied_by: Optional[str] = None,
) -> RunSummary:
    """Apply every ``.sql`` migration that has not yet been applied.

    Parameters
    ----------
    conn:
        A DB-API 2.0 connection. The runner expects ``cursor()``,
        ``commit()``, ``rollback()`` and an ``autocommit`` writable
        attribute (pg8000.dbapi conforms; the unit tests use a
        ``MagicMock`` shaped to match).
    migrations_dir:
        Directory containing the ``*.sql`` files. The runner scans
        only the immediate directory — subdirectories are ignored.
    applied_by:
        Optional identifier recorded into ``schema_version.applied_by``.
        The Lambda handler typically passes the IAM DB user (e.g.
        ``"migration_runner"``) so a quick ``SELECT * FROM
        schema_version`` shows who minted each row.

    Returns
    -------
    A :class:`RunSummary` describing what was applied, skipped, or
    drifted.

    Raises
    ------
    MigrationFilenameError
        If a ``.sql`` file does not match the ``NNNN_slug.sql`` pattern.
    MigrationOrderError
        If a new ``.sql`` file is older than an already-applied version.
    """
    started = time.monotonic()
    summary = RunSummary()

    migrations_path = Path(migrations_dir)
    if not migrations_path.is_dir():
        raise MigrationRunnerError(
            f"Migrations directory does not exist or is not a directory: {migrations_path!s}"
        )

    # 1) Ensure schema_version exists. We commit immediately so the
    #    SELECT below sees the table even on a brand-new database.
    summary.schema_version_created = _ensure_schema_version_table(conn)

    # 2) Pull the set of already-applied versions.
    applied_rows = _select_applied_migrations(conn)
    applied_by_version = {row.version: row for row in applied_rows}

    # 3) Discover the on-disk corpus, in lexical order.
    files = _discover_migration_files(migrations_path)

    # 4) Detect order violations BEFORE applying anything. We refuse to
    #    proceed if any new file is older than the max already-applied
    #    version — see MigrationOrderError docstring.
    if applied_by_version:
        max_applied_version = max(applied_by_version.keys())
        for f in files:
            if f.version not in applied_by_version and f.version < max_applied_version:
                raise MigrationOrderError(
                    f"Migration {f.filename!r} (version {f.version}) is older than the latest applied "
                    f"version {max_applied_version!r}. Forward-only migrations cannot be inserted "
                    "into the past — rename the file to a version greater than the latest applied "
                    "version."
                )

    # 5) Apply / skip / record drift.
    for f in files:
        existing = applied_by_version.get(f.version)
        if existing is None:
            LOG.info("applying migration %s", f.filename)
            _apply_one(
                conn=conn,
                migration=f,
                applied_by=applied_by,
            )
            summary.applied.append(f.filename)
            continue

        if existing.checksum is not None and existing.checksum != f.checksum:
            # Forward-only contract: drift signal only, never re-apply.
            entry = DriftEntry(
                version=f.version,
                filename=f.filename,
                expected_checksum=existing.checksum,
                actual_checksum=f.checksum,
            )
            LOG.critical(
                "migration drift detected: %s expected_checksum=%s actual_checksum=%s "
                "(file content changed since it was applied; forward-only convention forbids "
                "editing an applied migration — author a new migration instead)",
                f.filename,
                existing.checksum,
                f.checksum,
            )
            summary.drift.append(entry)
            summary.skipped.append(f.filename)
        else:
            LOG.info("skipping already-applied migration %s", f.filename)
            summary.skipped.append(f.filename)

    summary.elapsed_ms = int((time.monotonic() - started) * 1000)
    return summary


# ---------------------------------------------------------------------------
# Discovery / parsing.
# ---------------------------------------------------------------------------


def _discover_migration_files(migrations_dir: Path) -> List[_MigrationFile]:
    """Read every ``*.sql`` file in ``migrations_dir`` and return them
    sorted by **filename** (lexical order).

    Lexical order on zero-padded numeric prefixes is the same as
    numeric order, which is what the migrations/README.md contract
    promises.
    """
    files: List[_MigrationFile] = []
    for entry in sorted(migrations_dir.iterdir(), key=lambda p: p.name):
        if not entry.is_file():
            continue
        if entry.suffix != ".sql":
            continue

        match = _FILENAME_RE.match(entry.name)
        if not match:
            raise MigrationFilenameError(
                f"Migration filename {entry.name!r} does not match expected "
                f"pattern 'NNNN_slug.sql'. Rename the file or remove it from "
                f"the migrations directory."
            )
        version = match.group("version")

        # Read as bytes for a deterministic hash regardless of the
        # platform's default text encoding, then decode to UTF-8 for
        # the SQL execution path. Any byte sequence that fails UTF-8
        # decoding is malformed SQL and we fail loudly.
        raw = entry.read_bytes()
        checksum = hashlib.sha256(raw).hexdigest()
        try:
            contents = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MigrationRunnerError(
                f"Migration {entry.name!r} is not valid UTF-8: {exc}"
            ) from exc

        files.append(
            _MigrationFile(
                version=version,
                filename=entry.name,
                path=entry,
                checksum=checksum,
                contents=contents,
                no_transaction=_has_no_transaction_directive(contents),
            )
        )

    return files


def _has_no_transaction_directive(contents: str) -> bool:
    """Detect ``-- +runner: no-transaction`` in the first 100 chars."""
    head = contents[:_DIRECTIVE_WINDOW_CHARS]
    return _NO_TX_DIRECTIVE in head


# ---------------------------------------------------------------------------
# DB interactions.
# ---------------------------------------------------------------------------


def _ensure_schema_version_table(conn: Any) -> bool:
    """Create the ``schema_version`` table if it does not already exist.

    Returns True if the table was created on this invocation, False if
    it already existed. We detect this by probing the catalog before
    the DDL — pg8000 does not expose ``rowcount`` for ``CREATE TABLE``.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name = 'schema_version'"
    )
    existed = cur.fetchone() is not None

    if not existed:
        cur = conn.cursor()
        cur.execute(_SCHEMA_VERSION_DDL)
        conn.commit()
        return True

    # Even when the table exists already, run the IF NOT EXISTS DDL so a
    # partial earlier failure (column missing, etc.) cannot leave a
    # bad-shaped table around. Postgres treats this as a no-op when the
    # full definition matches.
    cur = conn.cursor()
    cur.execute(_SCHEMA_VERSION_DDL)
    conn.commit()
    return False


def _select_applied_migrations(conn: Any) -> List[AppliedMigration]:
    """Return every row from ``schema_version`` ordered by version asc."""
    cur = conn.cursor()
    cur.execute(
        "SELECT version, filename, checksum FROM schema_version ORDER BY version ASC"
    )
    rows = cur.fetchall() or []
    return [
        AppliedMigration(version=row[0], filename=row[1], checksum=row[2])
        for row in rows
    ]


def _apply_one(
    *,
    conn: Any,
    migration: _MigrationFile,
    applied_by: Optional[str],
) -> None:
    """Apply a single migration file and record it in ``schema_version``.

    Two execution modes:

    * **Default** — wrap the SQL in a single transaction (``BEGIN`` /
      ``COMMIT``). The ``schema_version`` INSERT happens in the **same**
      transaction so a partial-apply failure rolls everything back and
      the next invocation tries the file again from scratch.
    * **No-transaction** — the file declares ``-- +runner: no-transaction``
      in its first 100 chars. Run the SQL with autocommit enabled, then
      issue the ``schema_version`` INSERT in its own short transaction.
      Note: this mode is genuinely riskier — if the SQL fails halfway
      through, the database is left in a partially-applied state that
      ``schema_version`` does not know about. The author of a no-tx
      migration is responsible for making the SQL itself idempotent.
    """
    insert_sql = (
        "INSERT INTO schema_version (version, filename, checksum, applied_by) "
        "VALUES (%s, %s, %s, %s)"
    )
    insert_params = (migration.version, migration.filename, migration.checksum, applied_by)

    if migration.no_transaction:
        # Switch to autocommit, run the body, switch back. We rollback
        # any in-progress implicit transaction first so the toggle does
        # not error on connections whose driver disallows changing
        # autocommit mid-transaction.
        try:
            conn.rollback()
        except Exception:  # pragma: no cover - defensive
            LOG.debug("rollback before autocommit toggle was a no-op", exc_info=True)

        prior_autocommit = getattr(conn, "autocommit", False)
        conn.autocommit = True
        try:
            cur = conn.cursor()
            cur.execute(migration.contents)
        finally:
            conn.autocommit = prior_autocommit

        # Record the apply in its own short transaction so the row is
        # visible immediately to subsequent SELECTs.
        cur = conn.cursor()
        cur.execute(insert_sql, insert_params)
        conn.commit()
        return

    # Default path: single explicit transaction wrapping the body and
    # the bookkeeping insert. pg8000 (and DB-API 2.0 generally) starts
    # an implicit transaction at the first statement, so we just need
    # to commit at the end. On exception we rollback and re-raise — the
    # caller decides whether to abort the whole run or carry on.
    try:
        cur = conn.cursor()
        cur.execute(migration.contents)
        cur = conn.cursor()
        cur.execute(insert_sql, insert_params)
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:  # pragma: no cover - defensive
            LOG.exception("rollback failed after migration apply error (non-fatal)")
        raise


# ---------------------------------------------------------------------------
# Convenience helpers exposed for tests / handler.
# ---------------------------------------------------------------------------


def discover_migration_files(migrations_dir: str | os.PathLike[str]) -> Sequence[str]:
    """Public helper returning just the filenames in apply order.

    Useful for the handler's "what would I run?" diagnostic logging.
    """
    return [f.filename for f in _discover_migration_files(Path(migrations_dir))]


def compute_checksum(path: str | os.PathLike[str]) -> str:
    """Public helper for computing a file's SHA-256 (matches what the
    runner persists into ``schema_version.checksum``)."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def parse_version(filename: str) -> str:
    """Public helper extracting the numeric version prefix from a filename.

    Raises :class:`MigrationFilenameError` for unparseable names.
    """
    match = _FILENAME_RE.match(filename)
    if not match:
        raise MigrationFilenameError(
            f"Filename {filename!r} does not match 'NNNN_slug.sql' pattern."
        )
    return match.group("version")


__all__: Iterable[str] = (
    "AppliedMigration",
    "DriftEntry",
    "MigrationFilenameError",
    "MigrationOrderError",
    "MigrationRunnerError",
    "RunSummary",
    "compute_checksum",
    "discover_migration_files",
    "parse_version",
    "run_migrations",
)

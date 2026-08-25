"""DuckDB session assembly: DuckLake attach, source attaches, and maintenance.

One aggregation run works inside a single DuckDB session with fixed catalog
aliases, so every SQL statement is source-agnostic:

- ``metrics`` -- the metrics DuckLake (writable; Neon catalog + R2 data)
- ``rsc`` -- the connector's product database (Postgres, read-only)
- ``ops`` -- the analytics ops database (Postgres, read-only here; writes go
  through psycopg2 in ``ops_db``)
- ``logs`` -- a schema of views over the OpenObserve parquet (see
  ``log_views``)

Tests build the same aliases from local fixtures (a plain DuckDB database
attached as ``metrics``/``rsc``/``ops``), so the aggregation SQL under test is
byte-identical to production.
"""

import logging
from typing import Any

import duckdb

from imbue.analytics.errors import LakeAttachError
from imbue.analytics.errors import LakeInsertError
from imbue.analytics.errors import LakeMaintenanceError
from imbue.analytics.errors import SessionAssemblyError

logger = logging.getLogger(__name__)

# DuckDB extensions the production session needs. ``INSTALL`` fetches from the
# official extension repository on first use in a fresh container (Modal
# containers have egress); ``LOAD`` is a no-op when already loaded.
_SESSION_EXTENSIONS = ("ducklake", "postgres", "httpfs")


def quote_sql_literal(value: str) -> str:
    """Single-quote a value for embedding in a DuckDB statement (ATTACH strings etc.)."""
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def create_duckdb_session() -> Any:
    # In-memory session: all durable state lives in the attached catalogs.
    # Pinned to UTC so day bucketing (CAST(ts AS DATE)) is deterministic
    # regardless of the host's timezone.
    connection = duckdb.connect()
    connection.execute("SET TimeZone = 'UTC'")
    return connection


def install_session_extensions(connection: Any, extensions: tuple[str, ...] = _SESSION_EXTENSIONS) -> None:
    """Raises SessionAssemblyError when an extension cannot be installed or loaded."""
    for extension in extensions:
        try:
            connection.execute(f"INSTALL {extension}; LOAD {extension};")
        except duckdb.Error as e:
            raise SessionAssemblyError(f"Cannot install/load DuckDB extension {extension!r}") from e


def create_r2_secret(
    connection: Any, secret_name: str, key_id: str, secret: str, account_id: str, bucket: str
) -> None:
    """Register R2 credentials for one bucket as a scoped DuckDB secret.

    The session holds one R2 secret per bucket (metrics readwrite, logs
    read-only), so each secret is SCOPEd to its bucket's URL prefix and DuckDB
    picks the right credentials per path. Raises SessionAssemblyError when
    the secret cannot be registered.
    """
    try:
        connection.execute(
            f"CREATE OR REPLACE SECRET {secret_name} ("
            f" TYPE r2,"
            f" KEY_ID {quote_sql_literal(key_id)},"
            f" SECRET {quote_sql_literal(secret)},"
            f" ACCOUNT_ID {quote_sql_literal(account_id)},"
            f" SCOPE {quote_sql_literal(f'r2://{bucket}')}"
            f")"
        )
    except duckdb.Error as e:
        raise SessionAssemblyError(f"Cannot register R2 secret {secret_name!r}") from e


def attach_ducklake(connection: Any, alias: str, catalog_dsn: str, data_path: str) -> None:
    """Attach one DuckLake (Postgres catalog, R2 data path) under ``alias``.

    Raises LakeAttachError when the catalog is unreachable.
    """
    attach_target = quote_sql_literal(f"ducklake:postgres:{catalog_dsn}")
    try:
        connection.execute(
            f"ATTACH IF NOT EXISTS {attach_target} AS {alias} (DATA_PATH {quote_sql_literal(data_path)})"
        )
    except duckdb.Error as e:
        raise LakeAttachError(f"Cannot attach the {alias} DuckLake catalog") from e


def attach_postgres_readonly(connection: Any, alias: str, dsn: str) -> None:
    """Attach a Postgres database read-only under a fixed alias.

    Raises LakeAttachError when the database is unreachable.
    """
    try:
        connection.execute(f"ATTACH IF NOT EXISTS {quote_sql_literal(dsn)} AS {alias} (TYPE postgres, READ_ONLY)")
    except duckdb.Error as e:
        raise LakeAttachError(f"Cannot attach Postgres source {alias!r}") from e


# The raw landing tables for in-workspace collection: typed envelope columns
# plus the full original record as a JSON string, so workspace-side schema
# drift can never break collection. Transcript rows land in the transcripts
# lake; every other feed lands in the metrics lake. Both are deduplicated by
# (feed_source, event_id) at aggregation time, never at insert time -- a
# cursor-write failure just causes a re-collection that dedupes downstream.
_RAW_TABLE_COLUMNS = (
    " event_at TIMESTAMPTZ,"
    " event_id VARCHAR,"
    " event_type VARCHAR,"
    " event_source VARCHAR,"
    " feed_source VARCHAR,"
    " host_id VARCHAR,"
    " account_id VARCHAR,"
    " run_id VARCHAR,"
    " collected_at TIMESTAMPTZ,"
    " script_version VARCHAR,"
    " payload VARCHAR"
)

METRICS_RAW_EVENTS_TABLE = "metrics.raw.workspace_events"
TRANSCRIPTS_RAW_EVENTS_TABLE = "transcripts.raw.transcript_events"

RAW_TABLE_DDL_STATEMENTS = (
    "CREATE SCHEMA IF NOT EXISTS metrics.raw",
    f"CREATE TABLE IF NOT EXISTS {METRICS_RAW_EVENTS_TABLE} ({_RAW_TABLE_COLUMNS})",
)

TRANSCRIPTS_RAW_TABLE_DDL_STATEMENTS = (
    "CREATE SCHEMA IF NOT EXISTS transcripts.raw",
    f"CREATE TABLE IF NOT EXISTS {TRANSCRIPTS_RAW_EVENTS_TABLE} ({_RAW_TABLE_COLUMNS})",
)


def ensure_raw_tables(connection: Any, ddl_statements: tuple[str, ...]) -> None:
    """Raises LakeAttachError when the raw landing tables cannot be ensured."""
    for statement in ddl_statements:
        try:
            connection.execute(statement)
        except duckdb.Error as e:
            raise LakeAttachError(f"Cannot ensure raw landing table: {statement}") from e


def insert_raw_records(
    connection: Any,
    table: str,
    # (event_at, event_id, event_type, event_source, feed_source, host_id,
    #  account_id, run_id, collected_at, script_version, payload) per row.
    rows: list[tuple[Any, ...]],
) -> None:
    """Insert one workspace's validated batch in a single transaction.

    Raises LakeInsertError on failure (after rolling back), so the caller
    skips the cursor advance and the next run re-collects.
    """
    if not rows:
        return
    placeholders = ", ".join(["?"] * 11)
    try:
        connection.execute("BEGIN TRANSACTION")
        connection.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)
        connection.execute("COMMIT")
    except duckdb.Error as e:
        try:
            connection.execute("ROLLBACK")
        except duckdb.Error:
            logger.warning("Rollback after a failed raw insert into %s also failed", table)
        raise LakeInsertError(f"Cannot insert {len(rows)} rows into {table}") from e


def build_maintenance_statements(catalog_alias: str, snapshot_retention_days: int) -> list[str]:
    """The per-lake maintenance pass: flush, compact, expire, clean up.

    Expiring snapshots older than the retention window is what bounds both
    time travel and the physical persistence of deleted rows; cleanup then
    removes the data files no remaining snapshot references.
    """
    alias_literal = quote_sql_literal(catalog_alias)
    return [
        f"CALL ducklake_flush_inlined_data({alias_literal})",
        f"CALL ducklake_merge_adjacent_files({alias_literal})",
        (
            f"CALL ducklake_expire_snapshots({alias_literal},"
            f" older_than => now() - INTERVAL {snapshot_retention_days} DAY)"
        ),
        f"CALL ducklake_cleanup_old_files({alias_literal}, cleanup_all => true)",
    ]


def run_maintenance(connection: Any, catalog_alias: str, snapshot_retention_days: int) -> None:
    """Raises LakeMaintenanceError when a maintenance statement fails (so the run is recorded as failed)."""
    for statement in build_maintenance_statements(catalog_alias, snapshot_retention_days):
        try:
            connection.execute(statement)
        except duckdb.Error as e:
            raise LakeMaintenanceError(f"Lake maintenance statement failed: {statement}") from e

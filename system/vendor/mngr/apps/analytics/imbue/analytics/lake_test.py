import duckdb
import pytest
from inline_snapshot import snapshot

from imbue.analytics.errors import LakeAttachError
from imbue.analytics.errors import LakeMaintenanceError
from imbue.analytics.errors import SessionAssemblyError
from imbue.analytics.lake import attach_ducklake
from imbue.analytics.lake import attach_postgres_readonly
from imbue.analytics.lake import build_maintenance_statements
from imbue.analytics.lake import create_duckdb_session
from imbue.analytics.lake import create_r2_secret
from imbue.analytics.lake import install_session_extensions
from imbue.analytics.lake import quote_sql_literal
from imbue.analytics.lake import run_maintenance


class _FailingConnection:
    """A stand-in connection whose every statement fails, for the error-wrapping tests."""

    def execute(self, statement: str) -> None:
        raise duckdb.Error(f"boom-8271 while executing: {statement}")


class _RecordingConnection:
    """A stand-in connection that records every executed statement."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(statement)


def test_quote_sql_literal_wraps_plain_values() -> None:
    assert quote_sql_literal("analytics-metrics-dev") == snapshot("'analytics-metrics-dev'")


def test_quote_sql_literal_escapes_embedded_single_quotes() -> None:
    assert quote_sql_literal("pass'word") == snapshot("'pass''word'")


def test_session_assembly_helpers_wrap_duckdb_failures() -> None:
    # These wrappings are what guarantee every job failure lands in the
    # job_runs bookkeeping (run_recorded_job catches AnalyticsError only).
    with pytest.raises(SessionAssemblyError, match="extension"):
        install_session_extensions(_FailingConnection())
    with pytest.raises(SessionAssemblyError, match="R2 secret 'metrics_bucket_secret'"):
        create_r2_secret(
            _FailingConnection(),
            secret_name="metrics_bucket_secret",
            key_id="key",
            secret="value",
            account_id="account",
            bucket="bucket",
        )


def test_run_maintenance_wraps_duckdb_failures() -> None:
    with pytest.raises(LakeMaintenanceError, match="Lake maintenance statement failed"):
        run_maintenance(_FailingConnection(), catalog_alias="metrics", snapshot_retention_days=30)


def test_maintenance_statements_flush_merge_expire_and_cleanup_in_order() -> None:
    statements = build_maintenance_statements("metrics", snapshot_retention_days=30)

    assert statements == snapshot(
        [
            "CALL ducklake_flush_inlined_data('metrics')",
            "CALL ducklake_merge_adjacent_files('metrics')",
            "CALL ducklake_expire_snapshots('metrics', older_than => now() - INTERVAL 30 DAY)",
            "CALL ducklake_cleanup_old_files('metrics', cleanup_all => true)",
        ]
    )


def test_create_duckdb_session_pins_the_timezone_to_utc() -> None:
    session = create_duckdb_session()

    assert session.execute("SELECT current_setting('TimeZone')").fetchone() == ("UTC",)


def test_attach_postgres_readonly_disables_ctid_scans_after_attaching() -> None:
    # The shared tiers' analytics_reader role is column-scoped on
    # workspace_records, and reading the ctid system column needs table-level
    # SELECT, so the session must not parallelize Postgres scans by ctid.
    connection = _RecordingConnection()

    attach_postgres_readonly(connection, alias="rsc", dsn="postgresql://x/y")

    assert connection.statements == snapshot(
        [
            "ATTACH IF NOT EXISTS 'postgresql://x/y' AS rsc (TYPE postgres, READ_ONLY)",
            "SET pg_use_ctid_scan = false",
        ]
    )


def test_attach_helpers_wrap_duckdb_failures() -> None:
    with pytest.raises(LakeAttachError, match="metrics DuckLake catalog"):
        attach_ducklake(
            _FailingConnection(), alias="metrics", catalog_dsn="postgresql://x/y", data_path="r2://bucket/lake/"
        )
    with pytest.raises(LakeAttachError, match="Postgres source 'rsc'"):
        attach_postgres_readonly(_FailingConnection(), alias="rsc", dsn="postgresql://x/y")

"""Test helpers: a local fixture session with the production catalog aliases.

The aggregation SQL only sees the fixed aliases (``metrics``, ``rsc``,
``ops``, ``logs``), so tests attach plain in-memory DuckDB databases under the
same names and create the source tables the SQL reads. The statements under
test are byte-identical to production; only the attachments differ.
"""

from typing import Any

import duckdb

from imbue.analytics.lake import RAW_TABLE_DDL_STATEMENTS
from imbue.analytics.lake import TRANSCRIPTS_RAW_TABLE_DDL_STATEMENTS

_FIXTURE_TABLE_DDL = (
    # The connector product-DB tables the aggregation reads (columns limited
    # to what the SQL touches).
    "CREATE TABLE rsc.workspace_records (user_id VARCHAR, host_id VARCHAR, created_at TIMESTAMPTZ)",
    "CREATE TABLE rsc.account_attribution (user_id VARCHAR, created_at TIMESTAMPTZ)",
    "CREATE TABLE rsc.download_events (created_at TIMESTAMPTZ)",
    (
        "CREATE TABLE rsc.account_entitlements ("
        " user_id VARCHAR, plan_name VARCHAR, created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ,"
        " suspended_at TIMESTAMPTZ"
        ")"
    ),
    # shares.user_id carries the 32-hex share label, not the SuperTokens id
    # (see the share_enabled signal in aggregation.py).
    "CREATE TABLE rsc.shares (host_id VARCHAR, user_id VARCHAR, state VARCHAR, created_at TIMESTAMPTZ)",
    # The ops job log behind pipeline_health.
    (
        "CREATE TABLE ops.job_runs ("
        " job_name VARCHAR, started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ, is_success BOOLEAN"
        ")"
    ),
    # The ops collection audit behind collection_health (columns limited to
    # what the SQL touches).
    (
        "CREATE TABLE ops.collection_runs ("
        " host_id VARCHAR, account_id VARCHAR, started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ, outcome VARCHAR"
        ")"
    ),
    # Stand-ins for the log views (tables here; the parquet-parsing views have
    # their own tests in log_views_test.py).
    "CREATE SCHEMA logs",
    (
        "CREATE TABLE logs.http_requests ("
        " line_at TIMESTAMPTZ, user_id VARCHAR, method VARCHAR, path VARCHAR, status INTEGER, duration_ms DOUBLE,"
        " imbue_client VARCHAR"
        ")"
    ),
    (
        "CREATE TABLE logs.share_visits ("
        " line_at TIMESTAMPTZ, visitor_user_id VARCHAR, host_id VARCHAR,"
        " owner_share_label VARCHAR, workspace_domain VARCHAR, is_owner BOOLEAN"
        ")"
    ),
)


def build_fixture_analytics_session() -> Any:
    """An in-memory DuckDB session with empty fixture sources under the production aliases."""
    connection = duckdb.connect()
    # Mirror the production session's UTC pin (create_duckdb_session) so day
    # bucketing behaves identically under any host timezone.
    connection.execute("SET TimeZone = 'UTC'")
    connection.execute("ATTACH ':memory:' AS metrics")
    connection.execute("ATTACH ':memory:' AS transcripts")
    connection.execute("ATTACH ':memory:' AS rsc")
    connection.execute("ATTACH ':memory:' AS ops")
    for statement in _FIXTURE_TABLE_DDL:
        connection.execute(statement)
    # The raw landing tables use the production DDL verbatim, so the SQL under
    # test sees exactly the deployed column set.
    for statement in (*RAW_TABLE_DDL_STATEMENTS, *TRANSCRIPTS_RAW_TABLE_DDL_STATEMENTS):
        connection.execute(statement)
    return connection

"""Views over the OpenObserve log parquet, parsed into typed columns.

OpenObserve's data plane is zstd parquet in the tier's R2 bucket; our service
log lines (the connector's ``http_request`` access records and
``share_visit_authorized`` records) arrive inside it as JSON strings in the
record body. These views parse that JSON defensively: the emitting handler
(``modal_app_kit``'s ``ensure_info_log_handler``) prefixes each line with the
logging formatter's timestamp, so the JSON is extracted from the first ``{``
onward (a no-op for pure-JSON bodies); a malformed or foreign body simply
yields NULLs and is filtered out by the ``type`` predicate, never an error.

The parquet layout and the body/timestamp column names are OpenObserve
internals -- pinned here as parameters (with production defaults in
``settings``) so a version bump only means re-verifying and adjusting the
configuration, not the SQL.
"""

from typing import Any
from typing import Final

import duckdb

from imbue.analytics.errors import SessionAssemblyError
from imbue.analytics.lake import quote_sql_literal

# OpenObserve stores the record timestamp as microseconds since the epoch.
DEFAULT_TIMESTAMP_COLUMN: Final[str] = "_timestamp"

# The OTLP log record body lands in this column.
DEFAULT_BODY_COLUMN: Final[str] = "body"


def _parsed_lines_cte(parquet_glob: str, body_column: str, timestamp_column: str) -> str:
    # The payload starts at the body's first "{": the emitting log handler
    # prefixes the JSON record with an asctime, and strpos misses (0) leave
    # the body intact for TRY_CAST to reject as NULL.
    return (
        "WITH parsed AS ("
        f" SELECT to_timestamp(CAST({timestamp_column} AS BIGINT) / 1000000) AS line_at,"
        f" TRY_CAST(substr({body_column}, strpos({body_column}, '{{')) AS JSON) AS payload"
        f" FROM read_parquet({quote_sql_literal(parquet_glob)}, union_by_name=true)"
        ")"
    )


def _env_filter_predicate(env_filter: str) -> str:
    """The extra per-view predicate scoping lines to one env's ``minds_env`` stamp.

    Dev envs share one per-tier OpenObserve bucket, so their per-env analytics
    stacks filter on the env name each service stamps into its structured log
    lines. Blank (the shared tiers) means everything is included -- lines
    without the field too. A filtered view deliberately excludes pre-stamping
    lines (they carry no env identity and are already cross-env mixed).
    """
    if not env_filter:
        return ""
    return f" AND json_extract_string(payload, '$.minds_env') = {quote_sql_literal(env_filter)}"


def build_log_view_statements(
    parquet_glob: str, body_column: str, timestamp_column: str, env_filter: str
) -> list[str]:
    """The ``logs`` schema: one view per structured log record type we consume."""
    parsed = _parsed_lines_cte(parquet_glob, body_column, timestamp_column)
    env_predicate = _env_filter_predicate(env_filter)
    http_requests_view = (
        "CREATE OR REPLACE VIEW logs.http_requests AS "
        f"{parsed} "
        "SELECT line_at,"
        " json_extract_string(payload, '$.user') AS user_id,"
        " json_extract_string(payload, '$.method') AS method,"
        " json_extract_string(payload, '$.path') AS path,"
        " TRY_CAST(json_extract_string(payload, '$.status') AS INTEGER) AS status,"
        " TRY_CAST(json_extract_string(payload, '$.duration_ms') AS DOUBLE) AS duration_ms"
        " FROM parsed"
        " WHERE json_extract_string(payload, '$.type') = 'http_request'"
        f"{env_predicate}"
    )
    share_visits_view = (
        "CREATE OR REPLACE VIEW logs.share_visits AS "
        f"{parsed} "
        "SELECT line_at,"
        " json_extract_string(payload, '$.visitor_user_id') AS visitor_user_id,"
        " json_extract_string(payload, '$.host_id') AS host_id,"
        " json_extract_string(payload, '$.owner_share_label') AS owner_share_label,"
        " json_extract_string(payload, '$.workspace_domain') AS workspace_domain,"
        " TRY_CAST(json_extract_string(payload, '$.is_owner') AS BOOLEAN) AS is_owner"
        " FROM parsed"
        " WHERE json_extract_string(payload, '$.type') = 'share_visit_authorized'"
        f"{env_predicate}"
    )
    return [
        "CREATE SCHEMA IF NOT EXISTS logs",
        http_requests_view,
        share_visits_view,
    ]


def create_log_views(
    connection: Any, parquet_glob: str, body_column: str, timestamp_column: str, env_filter: str
) -> None:
    """Raises SessionAssemblyError when a view cannot be created (e.g. an unreadable parquet source)."""
    for statement in build_log_view_statements(parquet_glob, body_column, timestamp_column, env_filter):
        try:
            connection.execute(statement)
        except duckdb.Error as e:
            raise SessionAssemblyError(f"Cannot create log views over {parquet_glob!r}") from e

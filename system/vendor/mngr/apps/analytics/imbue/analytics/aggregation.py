"""Gold-table aggregation: product-DB and log-derived facts in the metrics lake.

Every statement is written against the fixed session aliases assembled by
``lake`` (``metrics``, ``rsc``, ``ops``, ``logs``), so the exact SQL that runs
in production also runs in tests against local fixtures.

Idempotency model:

- ``activity`` is windowed: each run deletes and recomputes the trailing
  window, so late-arriving log parquet and missed runs heal themselves.
- The dimension and small fact tables (``accounts``, ``funnel_daily``,
  ``pipeline_health``) are fully rewritten each run -- they are tiny, and a
  full rewrite is the simplest idempotent shape.

The definition of "active" is deliberately NOT made here: every candidate
signal lands as its own ``signal_type`` row and the cut happens at query time
(see specs/minds-analytics/spec.md).
"""

from datetime import date
from typing import Any

import duckdb
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from imbue.analytics.errors import AggregationError

# The gold schema plus windowed-table DDL. Statements must stay individually
# idempotent: aggregation reruns after any partial failure.
_GOLD_SCHEMA_STATEMENTS = (
    "CREATE SCHEMA IF NOT EXISTS metrics.gold",
    (
        "CREATE TABLE IF NOT EXISTS metrics.gold.activity ("
        " account_id VARCHAR,"
        " day DATE,"
        " signal_type VARCHAR,"
        " signal_count BIGINT"
        ")"
    ),
)

# Each activity signal is one SELECT producing (account_id, day, signal_type,
# signal_count) rows for the recompute window. Adding a signal is adding a
# SELECT here -- the "active" definition stays a query-time decision.
_ACTIVITY_SIGNAL_SELECTS = (
    # Any authenticated request to our services: the "app open" signal (the
    # desktop client polls sync endpoints while running).
    (
        "SELECT user_id AS account_id, CAST(line_at AS DATE) AS day, 'app_open' AS signal_type,"
        " count(*) AS signal_count"
        " FROM logs.http_requests"
        " WHERE user_id IS NOT NULL AND user_id != '' AND CAST(line_at AS DATE) >= DATE {window_start}"
        " GROUP BY 1, 2"
    ),
    # Visiting someone else's shared workspace (the visitor's activity; owner
    # self-visits are excluded -- the owner's own activity is already covered
    # by app_open).
    (
        "SELECT visitor_user_id AS account_id, CAST(line_at AS DATE) AS day, 'share_visit' AS signal_type,"
        " count(*) AS signal_count"
        " FROM logs.share_visits"
        " WHERE visitor_user_id IS NOT NULL AND visitor_user_id != ''"
        " AND NOT coalesce(is_owner, false)"
        " AND CAST(line_at AS DATE) >= DATE {window_start}"
        " GROUP BY 1, 2"
    ),
    # Creating a workspace (any provider kind that syncs a record).
    (
        "SELECT user_id AS account_id, CAST(created_at AS DATE) AS day, 'workspace_created' AS signal_type,"
        " count(*) AS signal_count"
        " FROM rsc.workspace_records"
        " WHERE CAST(created_at AS DATE) >= DATE {window_start}"
        " GROUP BY 1, 2"
    ),
    # Account creation.
    (
        "SELECT user_id AS account_id, CAST(created_at AS DATE) AS day, 'signup' AS signal_type,"
        " count(*) AS signal_count"
        " FROM rsc.account_attribution"
        " WHERE CAST(created_at AS DATE) >= DATE {window_start}"
        " GROUP BY 1, 2"
    ),
    # Explorer in-workspace signals (collected feeds; distinct event ids so
    # cursor replays never double-count). Chat messages sent through the UI.
    (
        "SELECT account_id, CAST(event_at AS DATE) AS day, 'workspace_chat_message' AS signal_type,"
        " count(DISTINCT event_id) AS signal_count"
        " FROM metrics.raw.workspace_events"
        " WHERE feed_source = 'client_activity' AND event_type = 'message'"
        " AND CAST(event_at AS DATE) >= DATE {window_start}"
        " GROUP BY 1, 2"
    ),
    # Git commits landing in explorer workspaces.
    (
        "SELECT account_id, CAST(event_at AS DATE) AS day, 'workspace_git_commit' AS signal_type,"
        " count(DISTINCT event_id) AS signal_count"
        " FROM metrics.raw.workspace_events"
        " WHERE feed_source = 'git_numstat' AND event_type = 'git_commit'"
        " AND CAST(event_at AS DATE) >= DATE {window_start}"
        " GROUP BY 1, 2"
    ),
    # User messages in collected (redacted) transcripts -- the calibration
    # signal for extrapolating fleet-wide usage from app_open.
    (
        "SELECT account_id, CAST(event_at AS DATE) AS day, 'workspace_user_message' AS signal_type,"
        " count(DISTINCT event_id) AS signal_count"
        " FROM transcripts.raw.transcript_events"
        " WHERE event_type = 'user_message'"
        " AND CAST(event_at AS DATE) >= DATE {window_start}"
        " GROUP BY 1, 2"
    ),
)

_ACCOUNTS_STATEMENT = (
    "CREATE OR REPLACE TABLE metrics.gold.accounts AS"
    " SELECT user_id AS account_id, plan_name AS plan, created_at, updated_at"
    " FROM rsc.account_entitlements"
)

_FUNNEL_STATEMENT = (
    "CREATE OR REPLACE TABLE metrics.gold.funnel_daily AS"
    " WITH downloads AS ("
    "  SELECT CAST(created_at AS DATE) AS day, count(*) AS downloads"
    "  FROM rsc.download_events GROUP BY 1"
    " ), signups AS ("
    "  SELECT CAST(created_at AS DATE) AS day, count(*) AS signups"
    "  FROM rsc.account_attribution GROUP BY 1"
    " ), first_workspaces AS ("
    "  SELECT first_day AS day, count(*) AS first_workspaces FROM ("
    "   SELECT user_id, CAST(min(created_at) AS DATE) AS first_day"
    "   FROM rsc.workspace_records GROUP BY 1"
    "  ) GROUP BY 1"
    " )"
    " SELECT"
    "  coalesce(downloads.day, signups.day, first_workspaces.day) AS day,"
    "  coalesce(downloads.downloads, 0) AS downloads,"
    "  coalesce(signups.signups, 0) AS signups,"
    "  coalesce(first_workspaces.first_workspaces, 0) AS first_workspaces"
    " FROM downloads"
    " FULL OUTER JOIN signups ON signups.day = downloads.day"
    " FULL OUTER JOIN first_workspaces"
    "  ON first_workspaces.day = coalesce(downloads.day, signups.day)"
    " ORDER BY day"
)

# The downstream transcript-metrics derivation: turns, tool mix, and error
# rates per account and day, computed entirely from the transcripts lake into
# the metrics lake -- iterating on these tables never touches a workspace.
# Rows are deduped per account by event id first (cursor replays re-collect;
# event ids are workspace-generated, so one account's ids must never suppress
# another account's rows).
_TRANSCRIPT_DEDUPED_CTE = (
    "WITH deduped AS ("
    " SELECT * FROM transcripts.raw.transcript_events"
    " QUALIFY row_number() OVER (PARTITION BY account_id, event_id ORDER BY collected_at DESC) = 1"
    ")"
)

_TRANSCRIPT_DAILY_STATEMENT = (
    "CREATE OR REPLACE TABLE metrics.gold.transcript_daily AS "
    f"{_TRANSCRIPT_DEDUPED_CTE}"
    " SELECT account_id, CAST(event_at AS DATE) AS day,"
    "  count(*) FILTER (WHERE event_type = 'user_message') AS user_message_count,"
    "  count(*) FILTER (WHERE event_type = 'assistant_message') AS assistant_message_count,"
    "  count(*) FILTER (WHERE event_type = 'tool_result') AS tool_result_count,"
    "  count(*) FILTER ("
    "   WHERE event_type = 'tool_result'"
    "   AND TRY_CAST(json_extract_string(payload, '$.is_error') AS BOOLEAN)"
    "  ) AS tool_error_count,"
    "  count(DISTINCT json_extract_string(payload, '$.tool_name'))"
    "   FILTER (WHERE event_type = 'tool_result') AS distinct_tool_count,"
    "  count(DISTINCT json_extract_string(payload, '$.agent_id')) AS active_agent_count"
    " FROM deduped"
    " GROUP BY 1, 2"
    " ORDER BY account_id, day"
)

_TRANSCRIPT_TOOLS_DAILY_STATEMENT = (
    "CREATE OR REPLACE TABLE metrics.gold.transcript_tools_daily AS "
    f"{_TRANSCRIPT_DEDUPED_CTE}"
    " SELECT account_id, CAST(event_at AS DATE) AS day,"
    "  json_extract_string(payload, '$.tool_name') AS tool_name,"
    "  count(*) AS tool_result_count,"
    "  count(*) FILTER (WHERE TRY_CAST(json_extract_string(payload, '$.is_error') AS BOOLEAN)) AS tool_error_count"
    " FROM deduped"
    " WHERE event_type = 'tool_result' AND json_extract_string(payload, '$.tool_name') IS NOT NULL"
    " GROUP BY 1, 2, 3"
    " ORDER BY account_id, day, tool_name"
)

# Per-workspace collection health from the ops audit: staleness, consecutive
# failures, and the last outcome -- "collection from this workspace keeps
# failing" is a queryable metric, not a log grep.
_COLLECTION_HEALTH_STATEMENT = (
    "CREATE OR REPLACE TABLE metrics.gold.collection_health AS"
    " WITH runs AS ("
    "  SELECT host_id, account_id, started_at, finished_at, outcome,"
    "   max(CASE WHEN outcome = 'ok' THEN started_at END)"
    "    OVER (PARTITION BY host_id) AS last_success_started_at"
    "  FROM ops.collection_runs"
    " )"
    " SELECT host_id,"
    "  arg_max(account_id, started_at) AS account_id,"
    "  max(CASE WHEN outcome = 'ok' THEN finished_at END) AS last_success_at,"
    "  max(finished_at) AS last_attempt_at,"
    "  count(*) FILTER ("
    "   WHERE outcome != 'ok' AND (last_success_started_at IS NULL OR started_at > last_success_started_at)"
    "  ) AS consecutive_failures,"
    "  arg_max(outcome, started_at) AS last_outcome"
    " FROM runs"
    " GROUP BY host_id"
    " ORDER BY host_id"
)

# Health of the pipeline itself, from the ops job log: staleness, consecutive
# failures since the last success, and the last run's duration (so "the cron
# is approaching its time budget" is a queryable metric before it is an
# outage).
_PIPELINE_HEALTH_STATEMENT = (
    "CREATE OR REPLACE TABLE metrics.gold.pipeline_health AS"
    " WITH runs AS ("
    "  SELECT job_name, started_at, finished_at, is_success,"
    "   max(CASE WHEN is_success THEN started_at END) OVER (PARTITION BY job_name) AS last_success_started_at"
    "  FROM ops.job_runs"
    " )"
    " SELECT job_name,"
    "  max(CASE WHEN is_success THEN finished_at END) AS last_success_at,"
    "  max(finished_at) AS last_run_at,"
    "  count(*) FILTER ("
    "   WHERE NOT is_success AND (last_success_started_at IS NULL OR started_at > last_success_started_at)"
    "  ) AS consecutive_failures,"
    "  arg_max(epoch(finished_at - started_at), started_at) AS last_duration_seconds"
    " FROM runs"
    " GROUP BY job_name"
    " ORDER BY job_name"
)


class AggregationCounters(BaseModel):
    """Row counts written by one aggregation run, for the cron's summary log line."""

    model_config = ConfigDict(frozen=True)

    activity_rows: int = Field(description="Rows in the recomputed activity window")
    account_rows: int = Field(description="Rows in the accounts dimension")
    funnel_rows: int = Field(description="Rows in funnel_daily")
    pipeline_health_rows: int = Field(description="Rows in pipeline_health")
    transcript_daily_rows: int = Field(description="Rows in transcript_daily")
    collection_health_rows: int = Field(description="Rows in collection_health")


def build_activity_statements(window_start: date) -> list[str]:
    """The windowed delete-and-recompute for the activity table."""
    window_literal = f"'{window_start.isoformat()}'"
    signal_union = " UNION ALL ".join(
        select.format(window_start=window_literal) for select in _ACTIVITY_SIGNAL_SELECTS
    )
    return [
        f"DELETE FROM metrics.gold.activity WHERE day >= DATE {window_literal}",
        f"INSERT INTO metrics.gold.activity {signal_union}",
    ]


def build_aggregation_statements(window_start: date) -> list[str]:
    return [
        *_GOLD_SCHEMA_STATEMENTS,
        *build_activity_statements(window_start),
        _ACCOUNTS_STATEMENT,
        _FUNNEL_STATEMENT,
        _PIPELINE_HEALTH_STATEMENT,
        _TRANSCRIPT_DAILY_STATEMENT,
        _TRANSCRIPT_TOOLS_DAILY_STATEMENT,
        _COLLECTION_HEALTH_STATEMENT,
    ]


def _count_rows(connection: Any, table: str) -> int:
    row = connection.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert row is not None, f"count(*) over {table} returned no row"
    return int(row[0])


def run_aggregation(connection: Any, window_start: date) -> AggregationCounters:
    """Rewrite the gold tables in the attached ``metrics`` lake.

    Raises AggregationError when any statement fails; a rerun after a partial
    failure converges (every statement is idempotent).
    """
    for statement in build_aggregation_statements(window_start):
        try:
            connection.execute(statement)
        except duckdb.Error as e:
            raise AggregationError(f"Aggregation statement failed: {statement[:120]}...") from e
    return AggregationCounters(
        activity_rows=_count_rows(connection, "metrics.gold.activity"),
        account_rows=_count_rows(connection, "metrics.gold.accounts"),
        funnel_rows=_count_rows(connection, "metrics.gold.funnel_daily"),
        pipeline_health_rows=_count_rows(connection, "metrics.gold.pipeline_health"),
        transcript_daily_rows=_count_rows(connection, "metrics.gold.transcript_daily"),
        collection_health_rows=_count_rows(connection, "metrics.gold.collection_health"),
    )

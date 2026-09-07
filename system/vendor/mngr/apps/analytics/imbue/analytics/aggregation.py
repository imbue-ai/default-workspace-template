"""Gold-table aggregation: product-DB and log-derived facts in the metrics lake.

Every statement is written against the fixed session aliases assembled by
``lake`` (``metrics``, ``rsc``, ``ops``, ``logs``), so the exact SQL that runs
in production also runs in tests against local fixtures.

Idempotency model:

- ``activity`` and ``client_versions_hourly`` are windowed: each run deletes
  and recomputes the trailing window, so late-arriving log parquet and missed
  runs heal themselves.
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
    # The signup dimension statically backfilled from SuperTokens on shared
    # tiers (docs/bringup.md section 7). Ensured here so the signup-timestamp
    # coalesce below always has the table to read; where no backfill ran (dev
    # envs, tests) it stays empty and attribution alone supplies signups.
    "CREATE TABLE IF NOT EXISTS metrics.gold.accounts_signup (account_id VARCHAR, joined_at TIMESTAMPTZ)",
    (
        "CREATE TABLE IF NOT EXISTS metrics.gold.client_versions_hourly ("
        " account_id VARCHAR,"
        " hour TIMESTAMPTZ,"
        " imbue_client VARCHAR,"
        " request_count BIGINT"
        ")"
    ),
)

# The signup-timestamp rule (docs/bringup.md section 7): SuperTokens truth
# where the static backfill has it, attribution otherwise. account_attribution
# is written at account creation on every path since 2026-08-17, so the union
# covers accounts created after the backfill.
_SIGNUP_MOMENTS_SUBQUERY = (
    "SELECT account_id, joined_at AS signup_at FROM metrics.gold.accounts_signup"
    " UNION ALL"
    " SELECT user_id AS account_id, created_at AS signup_at FROM rsc.account_attribution"
    " WHERE user_id NOT IN (SELECT account_id FROM metrics.gold.accounts_signup)"
)

# Every account id any signup-shaped source knows: the spine for the accounts
# dimension and for mapping share labels back to full SuperTokens ids.
_ACCOUNT_ID_SPINE_SUBQUERY = (
    "SELECT account_id FROM metrics.gold.accounts_signup"
    " UNION SELECT user_id AS account_id FROM rsc.account_entitlements"
    " UNION SELECT user_id AS account_id FROM rsc.account_attribution"
)


def _turn_predicate(legacy_event_type: str, step_source: str) -> str:
    """The SQL predicate matching one kind of turn in either stream vintage.

    Every derivation below counts turns and tool results across both, because a workspace can
    hold agents of either: an ATIF `step` discriminated by its `source` is what a legacy
    `user_message` / `assistant_message` record was, and one entry of an ATIF `observation`
    record's `results[]` is what a legacy `tool_result` record was.
    """
    return (
        f"(event_type = '{legacy_event_type}'"
        f" OR (event_type = 'step' AND json_extract_string(payload, '$.source') = '{step_source}'))"
    )


_IS_USER_TURN = _turn_predicate("user_message", "user")
_IS_AGENT_TURN = _turn_predicate("assistant_message", "agent")

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
    # Account creation, on the coalesced signup timestamp (see
    # _SIGNUP_MOMENTS_SUBQUERY).
    (
        "SELECT account_id, CAST(signup_at AS DATE) AS day, 'signup' AS signal_type,"
        " count(*) AS signal_count"
        " FROM (" + _SIGNUP_MOMENTS_SUBQUERY + ")"
        " WHERE CAST(signup_at AS DATE) >= DATE {window_start}"
        " GROUP BY 1, 2"
    ),
    # Enabling sharing for a workspace. shares.user_id stores the 32-hex share
    # label (the SuperTokens id lowercased with hyphens stripped -- the
    # connector's derive_share_user_label), so it maps back to the full id
    # through the known-account spine. created_at marks the first enablement;
    # re-shares only rotate the token and touch updated_at.
    (
        "SELECT ids.account_id, CAST(shares.created_at AS DATE) AS day, 'share_enabled' AS signal_type,"
        " count(*) AS signal_count"
        " FROM rsc.shares AS shares"
        " JOIN (" + _ACCOUNT_ID_SPINE_SUBQUERY + ") AS ids"
        "  ON replace(lower(ids.account_id), '-', '') = shares.user_id"
        " WHERE CAST(shares.created_at AS DATE) >= DATE {window_start}"
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
    # Git commits landing in explorer workspaces. The signal means new code
    # produced IN the workspace, but every workspace clone carries the
    # template repo's commit history (and update-self pulls more of it), so a
    # commit sha seen in more than one workspace is upstream history, not the
    # user's work. The multi-workspace scan is deliberately unwindowed:
    # template commits dated inside the window stay excluded.
    (
        "SELECT account_id, CAST(event_at AS DATE) AS day, 'workspace_git_commit' AS signal_type,"
        " count(DISTINCT event_id) AS signal_count"
        " FROM metrics.raw.workspace_events"
        " WHERE feed_source = 'git_numstat' AND event_type = 'git_commit'"
        " AND CAST(event_at AS DATE) >= DATE {window_start}"
        " AND event_id IN ("
        "  SELECT event_id FROM metrics.raw.workspace_events"
        "  WHERE feed_source = 'git_numstat'"
        "  GROUP BY event_id HAVING count(DISTINCT host_id) = 1"
        " )"
        " GROUP BY 1, 2"
    ),
    # User messages in collected (redacted) transcripts -- the calibration
    # signal for extrapolating fleet-wide usage from app_open.
    (
        "SELECT account_id, CAST(event_at AS DATE) AS day, 'workspace_user_message' AS signal_type,"
        " count(DISTINCT event_id) AS signal_count"
        " FROM transcripts.raw.transcript_events"
        f" WHERE {_IS_USER_TURN}"
        " AND CAST(event_at AS DATE) >= DATE {window_start}"
        " GROUP BY 1, 2"
    ),
)

# The fleet-version picture: which client version each account's requests
# carried, hour by hour, so a release rollout (or a staged-rollout halt) is
# observable as it happens. The desktop client polls sync endpoints about
# once a minute while it runs, so any hour a client was open is represented.
# The raw ``X-Imbue-Client`` identifier is kept verbatim (e.g. "minds/0.4.2
# imbue-cloud-plugin/0.1.6"); parsing out the product version is a query-time
# decision, like the "active" cut. Lines without the header (clients older
# than minds 0.4.1, and pre-header log history) land in the '' bucket rather
# than being dropped, so the unversioned share of the fleet stays visible.
_CLIENT_VERSIONS_HOURLY_SELECT = (
    "SELECT user_id AS account_id,"
    " date_trunc('hour', line_at) AS hour,"
    " coalesce(imbue_client, '') AS imbue_client,"
    " count(*) AS request_count"
    " FROM logs.http_requests"
    " WHERE user_id IS NOT NULL AND user_id != ''"
    " AND CAST(line_at AS DATE) >= DATE {window_start}"
    " GROUP BY 1, 2, 3"
)

# The account dimension spans every id any signup source knows, not just the
# lazily-created entitlements rows: plan is NULL until the account's first
# quota-relevant request creates its entitlements row, signup_at follows the
# section-7 coalesce rule (entitlements.created_at is the lazy-creation
# moment, not the signup), and is_suspended lets reports exclude
# operator-suspended accounts at query time.
_ACCOUNTS_STATEMENT = (
    "CREATE OR REPLACE TABLE metrics.gold.accounts AS"
    " WITH ids AS (" + _ACCOUNT_ID_SPINE_SUBQUERY + ")"
    " SELECT ids.account_id,"
    "  entitlements.plan_name AS plan,"
    "  coalesce(signup.joined_at, attribution.created_at) AS signup_at,"
    "  entitlements.suspended_at IS NOT NULL AS is_suspended,"
    "  entitlements.created_at AS entitlements_created_at,"
    "  entitlements.updated_at AS entitlements_updated_at"
    " FROM ids"
    " LEFT JOIN metrics.gold.accounts_signup AS signup ON signup.account_id = ids.account_id"
    " LEFT JOIN rsc.account_entitlements AS entitlements ON entitlements.user_id = ids.account_id"
    " LEFT JOIN rsc.account_attribution AS attribution ON attribution.user_id = ids.account_id"
    " ORDER BY ids.account_id"
)

# The funnel is written over a full day spine (first source day through last),
# so days where nothing happened appear as zeros instead of silent gaps.
_FUNNEL_STATEMENT = (
    "CREATE OR REPLACE TABLE metrics.gold.funnel_daily AS"
    " WITH downloads AS ("
    "  SELECT CAST(created_at AS DATE) AS day, count(*) AS downloads"
    "  FROM rsc.download_events GROUP BY 1"
    " ), signups AS ("
    "  SELECT CAST(signup_at AS DATE) AS day, count(*) AS signups"
    "  FROM (" + _SIGNUP_MOMENTS_SUBQUERY + ") GROUP BY 1"
    " ), first_workspaces AS ("
    "  SELECT first_day AS day, count(*) AS first_workspaces FROM ("
    "   SELECT user_id, CAST(min(created_at) AS DATE) AS first_day"
    "   FROM rsc.workspace_records GROUP BY 1"
    "  ) GROUP BY 1"
    " ), bounds AS ("
    "  SELECT min(day) AS min_day, max(day) AS max_day FROM ("
    "   SELECT day FROM downloads"
    "   UNION ALL SELECT day FROM signups"
    "   UNION ALL SELECT day FROM first_workspaces"
    "  )"
    " ), days AS ("
    "  SELECT CAST(unnest(generate_series(min_day, max_day, INTERVAL 1 DAY)) AS DATE) AS day"
    "  FROM bounds WHERE min_day IS NOT NULL"
    " )"
    " SELECT days.day,"
    "  coalesce(downloads.downloads, 0) AS downloads,"
    "  coalesce(signups.signups, 0) AS signups,"
    "  coalesce(first_workspaces.first_workspaces, 0) AS first_workspaces"
    " FROM days"
    " LEFT JOIN downloads USING (day)"
    " LEFT JOIN signups USING (day)"
    " LEFT JOIN first_workspaces USING (day)"
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

# One row per tool result, from either vintage. A system step's inline
# observation is deliberately not a tool result (it carries compaction output,
# not a tool call's), matching the legacy shape, which had no counterpart for it.
_TOOL_RESULTS_CTE = (
    ", tool_results AS ("
    "  SELECT account_id, event_at,"
    "   json_extract_string(payload, '$.tool_name') AS tool_name,"
    "   TRY_CAST(json_extract_string(payload, '$.is_error') AS BOOLEAN) AS is_error"
    "  FROM deduped WHERE event_type = 'tool_result'"
    "  UNION ALL"
    "  SELECT account_id, event_at,"
    "   json_extract_string(result, '$.extra.tool_name') AS tool_name,"
    "   TRY_CAST(json_extract_string(result, '$.extra.is_error') AS BOOLEAN) AS is_error"
    "  FROM ("
    "   SELECT account_id, event_at, unnest(json_extract(payload, '$.results[*]')) AS result"
    "   FROM deduped WHERE event_type = 'observation'"
    "  )"
    " )"
)

_TRANSCRIPT_DAILY_STATEMENT = (
    "CREATE OR REPLACE TABLE metrics.gold.transcript_daily AS "
    f"{_TRANSCRIPT_DEDUPED_CTE}{_TOOL_RESULTS_CTE}"
    ", turn_counts AS ("
    "  SELECT account_id, CAST(event_at AS DATE) AS day,"
    f"   count(*) FILTER (WHERE {_IS_USER_TURN}) AS user_message_count,"
    f"   count(*) FILTER (WHERE {_IS_AGENT_TURN}) AS assistant_message_count,"
    "   count(DISTINCT json_extract_string(payload, '$.agent_id')) AS active_agent_count"
    "  FROM deduped GROUP BY 1, 2"
    " ), tool_counts AS ("
    "  SELECT account_id, CAST(event_at AS DATE) AS day,"
    "   count(*) AS tool_result_count,"
    "   count(*) FILTER (WHERE is_error) AS tool_error_count,"
    "   count(DISTINCT tool_name) AS distinct_tool_count"
    "  FROM tool_results GROUP BY 1, 2"
    " )"
    " SELECT turn_counts.account_id, turn_counts.day,"
    "  turn_counts.user_message_count, turn_counts.assistant_message_count,"
    "  coalesce(tool_counts.tool_result_count, 0) AS tool_result_count,"
    "  coalesce(tool_counts.tool_error_count, 0) AS tool_error_count,"
    "  coalesce(tool_counts.distinct_tool_count, 0) AS distinct_tool_count,"
    "  turn_counts.active_agent_count"
    # Every tool_results row comes from a deduped row, so its (account, day) is
    # always present on the left -- the join only fills in accounts with no tool use.
    " FROM turn_counts LEFT JOIN tool_counts"
    "  ON tool_counts.account_id = turn_counts.account_id AND tool_counts.day = turn_counts.day"
    " ORDER BY account_id, day"
)

_TRANSCRIPT_TOOLS_DAILY_STATEMENT = (
    "CREATE OR REPLACE TABLE metrics.gold.transcript_tools_daily AS "
    f"{_TRANSCRIPT_DEDUPED_CTE}{_TOOL_RESULTS_CTE}"
    " SELECT account_id, CAST(event_at AS DATE) AS day, tool_name,"
    "  count(*) AS tool_result_count,"
    "  count(*) FILTER (WHERE is_error) AS tool_error_count"
    " FROM tool_results"
    " WHERE tool_name IS NOT NULL"
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
    client_version_rows: int = Field(description="Rows in the recomputed client_versions_hourly window")
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


def build_client_versions_statements(window_start: date) -> list[str]:
    """The windowed delete-and-recompute for the client_versions_hourly table."""
    window_literal = f"'{window_start.isoformat()}'"
    return [
        f"DELETE FROM metrics.gold.client_versions_hourly WHERE hour >= DATE {window_literal}",
        "INSERT INTO metrics.gold.client_versions_hourly "
        + _CLIENT_VERSIONS_HOURLY_SELECT.format(window_start=window_literal),
    ]


def build_aggregation_statements(window_start: date) -> list[str]:
    return [
        *_GOLD_SCHEMA_STATEMENTS,
        *build_activity_statements(window_start),
        *build_client_versions_statements(window_start),
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
        client_version_rows=_count_rows(connection, "metrics.gold.client_versions_hourly"),
        account_rows=_count_rows(connection, "metrics.gold.accounts"),
        funnel_rows=_count_rows(connection, "metrics.gold.funnel_daily"),
        pipeline_health_rows=_count_rows(connection, "metrics.gold.pipeline_health"),
        transcript_daily_rows=_count_rows(connection, "metrics.gold.transcript_daily"),
        collection_health_rows=_count_rows(connection, "metrics.gold.collection_health"),
    )

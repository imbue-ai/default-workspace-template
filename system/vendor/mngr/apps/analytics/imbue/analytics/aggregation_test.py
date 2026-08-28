from datetime import date
from typing import Any

import pytest
from inline_snapshot import snapshot

from imbue.analytics.aggregation import run_aggregation
from imbue.analytics.errors import AggregationError
from imbue.analytics.testing import build_fixture_analytics_session

_WINDOW_START = date(2026, 8, 10)


def test_activity_aggregates_every_signal_source_per_account_and_day() -> None:
    session = build_fixture_analytics_session()
    session.execute(
        "INSERT INTO logs.http_requests VALUES"
        " ('2026-08-12 09:00:00+00', 'user-a', 'GET', '/account', 200, 10.0),"
        " ('2026-08-12 09:05:00+00', 'user-a', 'PUT', '/sync/records/h1', 200, 15.0),"
        " ('2026-08-12 09:10:00+00', '', 'GET', '/version', 200, 1.0)"
    )
    session.execute(
        "INSERT INTO logs.share_visits VALUES"
        " ('2026-08-13 10:00:00+00', 'user-b', 'host-x', 'lbl', 'd', false),"
        " ('2026-08-13 10:05:00+00', 'user-owner', 'host-x', 'lbl', 'd', true)"
    )
    session.execute("INSERT INTO rsc.workspace_records VALUES ('user-a', 'host-1', '2026-08-12 11:00:00+00')")
    session.execute("INSERT INTO rsc.account_attribution VALUES ('user-b', '2026-08-13 08:00:00+00')")

    run_aggregation(session, _WINDOW_START)
    activity_rows = session.execute(
        "SELECT account_id, CAST(day AS VARCHAR), signal_type, signal_count"
        " FROM metrics.gold.activity ORDER BY account_id, day, signal_type"
    ).fetchall()

    assert activity_rows == snapshot(
        [
            ("user-a", "2026-08-12", "app_open", 2),
            ("user-a", "2026-08-12", "workspace_created", 1),
            ("user-b", "2026-08-13", "share_visit", 1),
            ("user-b", "2026-08-13", "signup", 1),
        ]
    )


def test_activity_recompute_preserves_rows_older_than_the_window_and_is_idempotent() -> None:
    session = build_fixture_analytics_session()
    session.execute("INSERT INTO logs.http_requests VALUES ('2026-08-12 09:00:00+00', 'user-a', 'GET', '/', 200, 1.0)")
    run_aggregation(session, _WINDOW_START)
    # Simulate an aggregate written by an earlier run over a window that has
    # since aged out: the recompute must never touch it.
    session.execute("INSERT INTO metrics.gold.activity VALUES ('user-old', DATE '2026-07-01', 'app_open', 5)")

    run_aggregation(session, _WINDOW_START)
    run_aggregation(session, _WINDOW_START)
    all_rows = session.execute(
        "SELECT account_id, CAST(day AS VARCHAR), signal_type, signal_count FROM metrics.gold.activity ORDER BY day"
    ).fetchall()

    assert all_rows == snapshot(
        [
            ("user-old", "2026-07-01", "app_open", 5),
            ("user-a", "2026-08-12", "app_open", 1),
        ]
    )


def test_accounts_dimension_includes_entitlements_only_accounts_with_plans() -> None:
    session = build_fixture_analytics_session()
    session.execute(
        "INSERT INTO rsc.account_entitlements (user_id, plan_name, created_at, updated_at) VALUES"
        " ('user-a', 'explorer', '2026-08-01 00:00:00+00', '2026-08-02 00:00:00+00'),"
        " ('user-b', 'ally', '2026-08-03 00:00:00+00', '2026-08-03 00:00:00+00')"
    )

    run_aggregation(session, _WINDOW_START)
    account_rows = session.execute("SELECT account_id, plan FROM metrics.gold.accounts ORDER BY account_id").fetchall()

    assert account_rows == snapshot([("user-a", "explorer"), ("user-b", "ally")])


def test_accounts_dimension_spans_signup_sources_and_flags_suspension() -> None:
    session = build_fixture_analytics_session()
    # First run only ensures the gold schema (incl. the accounts_signup
    # backfill table) exists so the fixture rows below have somewhere to land.
    run_aggregation(session, _WINDOW_START)
    session.execute("INSERT INTO metrics.gold.accounts_signup VALUES ('user-a', '2026-08-01 09:00:00+00')")
    # user-a's entitlements row was lazily created days after the real signup,
    # and the account has since been suspended.
    session.execute(
        "INSERT INTO rsc.account_entitlements (user_id, plan_name, created_at, updated_at, suspended_at) VALUES"
        " ('user-a', 'explorer', '2026-08-05 00:00:00+00', '2026-08-06 00:00:00+00', '2026-08-07 00:00:00+00')"
    )
    # user-b signed up after the backfill and has no entitlements row yet.
    session.execute("INSERT INTO rsc.account_attribution VALUES ('user-b', '2026-08-12 08:00:00+00')")

    run_aggregation(session, _WINDOW_START)
    account_rows = session.execute(
        "SELECT account_id, plan, CAST(signup_at AS VARCHAR), is_suspended"
        " FROM metrics.gold.accounts ORDER BY account_id"
    ).fetchall()

    assert account_rows == snapshot(
        [
            ("user-a", "explorer", "2026-08-01 09:00:00+00", True),
            ("user-b", None, "2026-08-12 08:00:00+00", False),
        ]
    )


def test_signup_signal_prefers_the_supertokens_backfill_over_attribution() -> None:
    session = build_fixture_analytics_session()
    run_aggregation(session, _WINDOW_START)
    # user-a is in the backfill and also has a later attribution row: one
    # signup, on the backfill day. user-b exists only in attribution.
    session.execute("INSERT INTO metrics.gold.accounts_signup VALUES ('user-a', '2026-08-11 09:00:00+00')")
    session.execute("INSERT INTO rsc.account_attribution VALUES ('user-a', '2026-08-12 01:00:00+00')")
    session.execute("INSERT INTO rsc.account_attribution VALUES ('user-b', '2026-08-13 01:00:00+00')")

    run_aggregation(session, _WINDOW_START)
    signup_rows = session.execute(
        "SELECT account_id, CAST(day AS VARCHAR), signal_count FROM metrics.gold.activity"
        " WHERE signal_type = 'signup' ORDER BY account_id"
    ).fetchall()

    assert signup_rows == snapshot(
        [
            ("user-a", "2026-08-11", 1),
            ("user-b", "2026-08-13", 1),
        ]
    )


def test_share_enabled_signal_maps_share_labels_back_to_account_ids() -> None:
    session = build_fixture_analytics_session()
    session.execute(
        "INSERT INTO rsc.account_entitlements (user_id, plan_name, created_at, updated_at) VALUES"
        " ('AB-12', 'explorer', '2026-08-01 00:00:00+00', '2026-08-01 00:00:00+00')"
    )
    # The share row carries the label form (lowercased, hyphens stripped).
    session.execute("INSERT INTO rsc.shares VALUES ('host-1', 'ab12', 'active', '2026-08-12 10:00:00+00')")

    run_aggregation(session, _WINDOW_START)
    share_rows = session.execute(
        "SELECT account_id, CAST(day AS VARCHAR), signal_count FROM metrics.gold.activity"
        " WHERE signal_type = 'share_enabled'"
    ).fetchall()

    assert share_rows == snapshot([("AB-12", "2026-08-12", 1)])


def test_funnel_daily_joins_sources_that_do_not_share_days() -> None:
    session = build_fixture_analytics_session()
    session.execute("INSERT INTO rsc.download_events VALUES ('2026-08-11 01:00:00+00'), ('2026-08-11 02:00:00+00')")
    session.execute("INSERT INTO rsc.account_attribution VALUES ('user-a', '2026-08-12 01:00:00+00')")
    # user-a's first workspace is on the 13th; the second workspace on the
    # 14th must not count as another "first".
    session.execute(
        "INSERT INTO rsc.workspace_records VALUES"
        " ('user-a', 'host-1', '2026-08-13 01:00:00+00'),"
        " ('user-a', 'host-2', '2026-08-14 01:00:00+00')"
    )

    run_aggregation(session, _WINDOW_START)
    funnel_rows = session.execute(
        "SELECT CAST(day AS VARCHAR), downloads, signups, first_workspaces FROM metrics.gold.funnel_daily ORDER BY day"
    ).fetchall()

    assert funnel_rows == snapshot(
        [
            ("2026-08-11", 2, 0, 0),
            ("2026-08-12", 0, 1, 0),
            ("2026-08-13", 0, 0, 1),
        ]
    )


def test_funnel_daily_fills_gap_days_with_zeros() -> None:
    session = build_fixture_analytics_session()
    session.execute("INSERT INTO rsc.download_events VALUES ('2026-08-11 01:00:00+00')")
    session.execute("INSERT INTO rsc.workspace_records VALUES ('user-a', 'host-1', '2026-08-14 01:00:00+00')")

    run_aggregation(session, _WINDOW_START)
    funnel_rows = session.execute(
        "SELECT CAST(day AS VARCHAR), downloads, signups, first_workspaces FROM metrics.gold.funnel_daily ORDER BY day"
    ).fetchall()

    assert funnel_rows == snapshot(
        [
            ("2026-08-11", 1, 0, 0),
            ("2026-08-12", 0, 0, 0),
            ("2026-08-13", 0, 0, 0),
            ("2026-08-14", 0, 0, 1),
        ]
    )


def test_pipeline_health_counts_failures_since_the_last_success() -> None:
    session = build_fixture_analytics_session()
    session.execute(
        "INSERT INTO ops.job_runs VALUES"
        " ('aggregation', '2026-08-12 10:00:00+00', '2026-08-12 10:01:00+00', true),"
        " ('aggregation', '2026-08-12 11:00:00+00', '2026-08-12 11:02:00+00', false),"
        " ('aggregation', '2026-08-12 12:00:00+00', '2026-08-12 12:03:00+00', false),"
        " ('never_succeeded', '2026-08-12 10:00:00+00', '2026-08-12 10:00:30+00', false)"
    )

    run_aggregation(session, _WINDOW_START)
    health_rows = session.execute(
        "SELECT job_name, CAST(last_success_at AS VARCHAR), consecutive_failures, last_duration_seconds"
        " FROM metrics.gold.pipeline_health ORDER BY job_name"
    ).fetchall()

    assert health_rows == snapshot(
        [
            ("aggregation", "2026-08-12 10:01:00+00", 2, 180.0),
            ("never_succeeded", None, 1, 30.0),
        ]
    )


def test_run_aggregation_returns_row_counters() -> None:
    session = build_fixture_analytics_session()
    session.execute("INSERT INTO logs.http_requests VALUES ('2026-08-12 09:00:00+00', 'user-a', 'GET', '/', 200, 1.0)")
    session.execute(
        "INSERT INTO rsc.account_entitlements (user_id, plan_name, created_at, updated_at)"
        " VALUES ('user-a', 'explorer', now(), now())"
    )

    counters = run_aggregation(session, _WINDOW_START)

    assert counters.activity_rows == 1
    assert counters.account_rows == 1
    assert counters.funnel_rows == 0
    assert counters.pipeline_health_rows == 0


def test_run_aggregation_wraps_sql_failures_in_aggregation_error() -> None:
    session = build_fixture_analytics_session()
    session.execute("DROP TABLE rsc.workspace_records")

    with pytest.raises(AggregationError, match="Aggregation statement failed"):
        run_aggregation(session, _WINDOW_START)


def _insert_raw_event(
    session: Any,
    table: str,
    event_at: str,
    event_id: str,
    event_type: str,
    feed_source: str,
    account_id: str,
    payload: str,
    collected_at: str = "2026-08-12 12:00:00+00",
    host_id: str = "host-1",
) -> None:
    session.execute(
        f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_at,
            event_id,
            event_type,
            feed_source,
            feed_source,
            host_id,
            account_id,
            "run-1",
            collected_at,
            "hash",
            payload,
        ),
    )


def test_explorer_workspace_signals_join_activity_and_dedupe_replayed_events() -> None:
    session = build_fixture_analytics_session()
    # The same chat-message event collected twice (a cursor replay) counts once.
    for run_suffix in ("a", "b"):
        _insert_raw_event(
            session,
            "metrics.raw.workspace_events",
            "2026-08-12 09:00:00+00",
            "evt-chat-1",
            "message",
            "client_activity",
            "user-a",
            '{"client_id": "c1"}',
            collected_at=f"2026-08-12 12:00:0{0 if run_suffix == 'a' else 5}+00",
        )
    _insert_raw_event(
        session,
        "metrics.raw.workspace_events",
        "2026-08-12 10:00:00+00",
        "evt-commit-1",
        "git_commit",
        "git_numstat",
        "user-a",
        '{"insertions": 5}',
    )
    _insert_raw_event(
        session,
        "transcripts.raw.transcript_events",
        "2026-08-12 11:00:00+00",
        "evt-msg-1",
        "user_message",
        "transcripts",
        "user-a",
        '{"content": "[redacted]"}',
    )

    run_aggregation(session, _WINDOW_START)
    activity_rows = session.execute(
        "SELECT account_id, CAST(day AS VARCHAR), signal_type, signal_count"
        " FROM metrics.gold.activity ORDER BY signal_type"
    ).fetchall()

    assert activity_rows == snapshot(
        [
            ("user-a", "2026-08-12", "workspace_chat_message", 1),
            ("user-a", "2026-08-12", "workspace_git_commit", 1),
            ("user-a", "2026-08-12", "workspace_user_message", 1),
        ]
    )


def test_workspace_git_commits_shared_across_workspaces_are_excluded_as_template_history() -> None:
    session = build_fixture_analytics_session()
    # The same commit sha collected from two different workspaces is shared
    # template/upstream history, not code the user produced: excluded for
    # every account. user-a's unique sha is the only surviving signal.
    _insert_raw_event(
        session,
        "metrics.raw.workspace_events",
        "2026-08-12 10:00:00+00",
        "sha-template",
        "git_commit",
        "git_numstat",
        "user-a",
        '{"insertions": 1}',
        host_id="host-1",
    )
    _insert_raw_event(
        session,
        "metrics.raw.workspace_events",
        "2026-08-12 10:00:00+00",
        "sha-template",
        "git_commit",
        "git_numstat",
        "user-b",
        '{"insertions": 1}',
        host_id="host-2",
    )
    _insert_raw_event(
        session,
        "metrics.raw.workspace_events",
        "2026-08-12 11:00:00+00",
        "sha-own",
        "git_commit",
        "git_numstat",
        "user-a",
        '{"insertions": 5}',
        host_id="host-1",
    )

    run_aggregation(session, _WINDOW_START)
    git_rows = session.execute(
        "SELECT account_id, CAST(day AS VARCHAR), signal_count FROM metrics.gold.activity"
        " WHERE signal_type = 'workspace_git_commit' ORDER BY account_id"
    ).fetchall()

    assert git_rows == snapshot([("user-a", "2026-08-12", 1)])


def test_transcript_daily_derives_turns_tool_mix_and_errors_deduped() -> None:
    session = build_fixture_analytics_session()
    _insert_raw_event(
        session,
        "transcripts.raw.transcript_events",
        "2026-08-12 09:00:00+00",
        "evt-u1",
        "user_message",
        "transcripts",
        "user-a",
        '{"content": "[redacted]", "agent_id": "agent-1"}',
    )
    _insert_raw_event(
        session,
        "transcripts.raw.transcript_events",
        "2026-08-12 09:01:00+00",
        "evt-a1",
        "assistant_message",
        "transcripts",
        "user-a",
        '{"text": "ok", "agent_id": "agent-1"}',
    )
    # A tool result collected twice must count once; one distinct failing tool.
    for collected_at in ("2026-08-12 12:00:00+00", "2026-08-12 13:00:00+00"):
        _insert_raw_event(
            session,
            "transcripts.raw.transcript_events",
            "2026-08-12 09:02:00+00",
            "evt-r1",
            "tool_result",
            "transcripts",
            "user-a",
            '{"tool_name": "Bash", "is_error": true, "agent_id": "agent-1"}',
            collected_at=collected_at,
        )
    _insert_raw_event(
        session,
        "transcripts.raw.transcript_events",
        "2026-08-12 09:03:00+00",
        "evt-r2",
        "tool_result",
        "transcripts",
        "user-a",
        '{"tool_name": "Read", "is_error": false, "agent_id": "agent-2"}',
    )
    # Another account reusing one of user-a's (workspace-generated) event ids
    # must keep its own row: dedup is per account, never across accounts.
    _insert_raw_event(
        session,
        "transcripts.raw.transcript_events",
        "2026-08-12 09:00:00+00",
        "evt-u1",
        "user_message",
        "transcripts",
        "user-b",
        '{"content": "[redacted]", "agent_id": "agent-9"}',
    )

    run_aggregation(session, _WINDOW_START)

    daily_rows = session.execute(
        "SELECT account_id, CAST(day AS VARCHAR), user_message_count, assistant_message_count,"
        " tool_result_count, tool_error_count, distinct_tool_count, active_agent_count"
        " FROM metrics.gold.transcript_daily ORDER BY account_id"
    ).fetchall()
    assert daily_rows == snapshot(
        [
            ("user-a", "2026-08-12", 1, 1, 2, 1, 2, 2),
            ("user-b", "2026-08-12", 1, 0, 0, 0, 0, 1),
        ]
    )

    tool_rows = session.execute(
        "SELECT tool_name, tool_result_count, tool_error_count FROM metrics.gold.transcript_tools_daily"
        " ORDER BY tool_name"
    ).fetchall()
    assert tool_rows == snapshot([("Bash", 1, 1), ("Read", 1, 0)])


def test_collection_health_tracks_staleness_and_consecutive_failures() -> None:
    session = build_fixture_analytics_session()
    session.execute(
        "INSERT INTO ops.collection_runs VALUES"
        " ('host-1', 'user-a', '2026-08-12 09:00:00+00', '2026-08-12 09:01:00+00', 'ok'),"
        " ('host-1', 'user-a', '2026-08-12 10:00:00+00', '2026-08-12 10:01:00+00', 'ssh_refused'),"
        " ('host-1', 'user-a', '2026-08-12 11:00:00+00', '2026-08-12 11:01:00+00', 'ssh_refused'),"
        " ('host-2', 'user-b', '2026-08-12 09:30:00+00', '2026-08-12 09:31:00+00', 'ok')"
    )

    run_aggregation(session, _WINDOW_START)
    health_rows = session.execute(
        "SELECT host_id, account_id, consecutive_failures, last_outcome FROM metrics.gold.collection_health"
        " ORDER BY host_id"
    ).fetchall()

    assert health_rows == snapshot(
        [
            ("host-1", "user-a", 2, "ssh_refused"),
            ("host-2", "user-b", 0, "ok"),
        ]
    )

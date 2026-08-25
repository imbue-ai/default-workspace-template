import json
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

import duckdb
import pytest

from imbue.analytics.errors import SessionAssemblyError
from imbue.analytics.log_views import DEFAULT_BODY_COLUMN
from imbue.analytics.log_views import DEFAULT_TIMESTAMP_COLUMN
from imbue.analytics.log_views import create_log_views


def _utc_connection() -> Any:
    connection = duckdb.connect()
    connection.execute("SET TimeZone = 'UTC'")
    return connection


def _micros(moment: datetime) -> int:
    return int(moment.timestamp() * 1_000_000)


def _write_fixture_parquet(connection: Any, parquet_path: Path, rows: list[tuple[int, str]]) -> None:
    connection.execute("CREATE OR REPLACE TABLE fixture_lines (_timestamp BIGINT, body VARCHAR)")
    if rows:
        connection.executemany("INSERT INTO fixture_lines VALUES (?, ?)", rows)
    connection.execute(f"COPY fixture_lines TO '{parquet_path}' (FORMAT parquet)")


def test_log_views_parse_http_request_lines_and_ignore_foreign_and_malformed_bodies(tmp_path: Path) -> None:
    connection = _utc_connection()
    line_moment = datetime(2026, 8, 18, 12, 30, tzinfo=timezone.utc)
    rows = [
        (
            _micros(line_moment),
            json.dumps(
                {
                    "type": "http_request",
                    "user": "st-user-83920",
                    "method": "GET",
                    "path": "/account",
                    "status": 200,
                    "duration_ms": 12.5,
                }
            ),
        ),
        # An unauthenticated request: user is empty.
        (
            _micros(line_moment),
            json.dumps({"type": "http_request", "user": "", "method": "GET", "path": "/version", "status": 200}),
        ),
        # A foreign structured line and a plain-text line must both be ignored.
        (_micros(line_moment), json.dumps({"type": "some_other_event", "field": "value"})),
        (_micros(line_moment), "Handled something entirely unstructured"),
    ]
    _write_fixture_parquet(connection, tmp_path / "lines.parquet", rows)

    create_log_views(
        connection,
        parquet_glob=str(tmp_path / "*.parquet"),
        body_column=DEFAULT_BODY_COLUMN,
        timestamp_column=DEFAULT_TIMESTAMP_COLUMN,
        env_filter="",
    )
    parsed_rows = connection.execute(
        "SELECT user_id, method, path, status, duration_ms FROM logs.http_requests ORDER BY user_id"
    ).fetchall()

    assert parsed_rows == [
        ("", "GET", "/version", 200, None),
        ("st-user-83920", "GET", "/account", 200, 12.5),
    ]


def test_share_visit_view_extracts_visitor_and_workspace_fields(tmp_path: Path) -> None:
    connection = _utc_connection()
    line_moment = datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc)
    rows = [
        (
            _micros(line_moment),
            json.dumps(
                {
                    "type": "share_visit_authorized",
                    "visitor_user_id": "st-visitor-11111",
                    "host_id": "host-" + "a" * 32,
                    "owner_share_label": "b" * 32,
                    "workspace_domain": "host-aa.bbbb.us-east.example.com",
                    "is_owner": False,
                }
            ),
        ),
        (_micros(line_moment), json.dumps({"type": "http_request", "user": "someone", "status": 200})),
    ]
    _write_fixture_parquet(connection, tmp_path / "lines.parquet", rows)

    create_log_views(
        connection,
        parquet_glob=str(tmp_path / "*.parquet"),
        body_column=DEFAULT_BODY_COLUMN,
        timestamp_column=DEFAULT_TIMESTAMP_COLUMN,
        env_filter="",
    )
    visit_rows = connection.execute(
        "SELECT visitor_user_id, host_id, owner_share_label, workspace_domain, is_owner FROM logs.share_visits"
    ).fetchall()
    # CAST to VARCHAR: fetching TIMESTAMPTZ values into Python needs pytz,
    # which is deliberately not a dependency of this package.
    visit_timestamps = connection.execute("SELECT CAST(line_at AS VARCHAR) FROM logs.share_visits").fetchall()

    assert visit_rows == [
        ("st-visitor-11111", "host-" + "a" * 32, "b" * 32, "host-aa.bbbb.us-east.example.com", False),
    ]
    assert visit_timestamps[0][0].startswith("2026-08-17 09:00:00")


def test_log_views_parse_bodies_carrying_a_logging_formatter_prefix(tmp_path: Path) -> None:
    # In production the JSON record is emitted through a logging handler whose
    # formatter prefixes an asctime ("%(asctime)s %(message)s"), so the stored
    # body is "<timestamp> {json}" -- the views must still parse it.
    connection = _utc_connection()
    line_moment = datetime(2026, 8, 18, 12, 30, tzinfo=timezone.utc)
    prefixed_body = "2026-08-18 12:30:00,123 " + json.dumps(
        {"type": "http_request", "user": "st-user-83920", "method": "GET", "path": "/account", "status": 200}
    )
    _write_fixture_parquet(connection, tmp_path / "lines.parquet", [(_micros(line_moment), prefixed_body)])

    create_log_views(
        connection,
        parquet_glob=str(tmp_path / "*.parquet"),
        body_column=DEFAULT_BODY_COLUMN,
        timestamp_column=DEFAULT_TIMESTAMP_COLUMN,
        env_filter="",
    )
    parsed_rows = connection.execute("SELECT user_id, method, path, status FROM logs.http_requests").fetchall()

    assert parsed_rows == [("st-user-83920", "GET", "/account", 200)]


def test_log_views_tolerate_an_empty_parquet_source(tmp_path: Path) -> None:
    connection = _utc_connection()
    _write_fixture_parquet(connection, tmp_path / "lines.parquet", [])

    create_log_views(
        connection,
        parquet_glob=str(tmp_path / "*.parquet"),
        body_column=DEFAULT_BODY_COLUMN,
        timestamp_column=DEFAULT_TIMESTAMP_COLUMN,
        env_filter="",
    )

    assert connection.execute("SELECT count(*) FROM logs.http_requests").fetchone() == (0,)
    assert connection.execute("SELECT count(*) FROM logs.share_visits").fetchone() == (0,)


def test_create_log_views_wraps_an_unreadable_parquet_source(tmp_path: Path) -> None:
    connection = _utc_connection()

    with pytest.raises(SessionAssemblyError, match="Cannot create log views"):
        create_log_views(
            connection,
            parquet_glob=str(tmp_path / "missing" / "*.parquet"),
            body_column=DEFAULT_BODY_COLUMN,
            timestamp_column=DEFAULT_TIMESTAMP_COLUMN,
            env_filter="",
        )


def test_env_filter_scopes_both_views_to_one_envs_stamped_lines(tmp_path: Path) -> None:
    # Dev envs share one per-tier OpenObserve bucket; a filtered view keeps
    # only lines stamped with the env's own minds_env value -- pre-stamping
    # lines (no field) and other envs' lines are excluded alike.
    connection = _utc_connection()
    line_moment = datetime(2026, 8, 18, 12, 30, tzinfo=timezone.utc)
    rows = [
        (
            _micros(line_moment),
            json.dumps({"type": "http_request", "user": "u-mine", "status": 200, "minds_env": "dev-mine"}),
        ),
        (
            _micros(line_moment),
            json.dumps({"type": "http_request", "user": "u-other", "status": 200, "minds_env": "dev-other"}),
        ),
        (_micros(line_moment), json.dumps({"type": "http_request", "user": "u-unstamped", "status": 200})),
        (
            _micros(line_moment),
            json.dumps({"type": "share_visit_authorized", "visitor_user_id": "v-mine", "minds_env": "dev-mine"}),
        ),
        (
            _micros(line_moment),
            json.dumps({"type": "share_visit_authorized", "visitor_user_id": "v-other", "minds_env": "dev-other"}),
        ),
    ]
    _write_fixture_parquet(connection, tmp_path / "lines.parquet", rows)

    create_log_views(
        connection,
        parquet_glob=str(tmp_path / "*.parquet"),
        body_column=DEFAULT_BODY_COLUMN,
        timestamp_column=DEFAULT_TIMESTAMP_COLUMN,
        env_filter="dev-mine",
    )

    assert connection.execute("SELECT user_id FROM logs.http_requests").fetchall() == [("u-mine",)]
    assert connection.execute("SELECT visitor_user_id FROM logs.share_visits").fetchall() == [("v-mine",)]


def test_blank_env_filter_includes_stamped_and_unstamped_lines_alike(tmp_path: Path) -> None:
    connection = _utc_connection()
    line_moment = datetime(2026, 8, 18, 12, 30, tzinfo=timezone.utc)
    rows = [
        (
            _micros(line_moment),
            json.dumps({"type": "http_request", "user": "u-stamped", "status": 200, "minds_env": "staging"}),
        ),
        (_micros(line_moment), json.dumps({"type": "http_request", "user": "u-unstamped", "status": 200})),
    ]
    _write_fixture_parquet(connection, tmp_path / "lines.parquet", rows)

    create_log_views(
        connection,
        parquet_glob=str(tmp_path / "*.parquet"),
        body_column=DEFAULT_BODY_COLUMN,
        timestamp_column=DEFAULT_TIMESTAMP_COLUMN,
        env_filter="",
    )

    users = sorted(row[0] for row in connection.execute("SELECT user_id FROM logs.http_requests").fetchall())
    assert users == ["u-stamped", "u-unstamped"]

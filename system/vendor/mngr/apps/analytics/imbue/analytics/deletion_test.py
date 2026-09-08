from datetime import datetime
from datetime import timezone

import duckdb
import pytest

from imbue.analytics.deletion import count_account_transcript_rows
from imbue.analytics.deletion import delete_account_transcripts
from imbue.analytics.errors import DeletionError
from imbue.analytics.mock_ops_db_test import FakeOpsConnection
from imbue.analytics.testing import build_fixture_analytics_session

_NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def _insert_transcript_row(session: duckdb.DuckDBPyConnection, event_id: str, account_id: str) -> None:
    session.execute(
        "INSERT INTO transcripts.raw.transcript_events VALUES"
        " ('2026-08-12 09:00:00+00', ?, 'user_message', 'claude', 'transcripts',"
        "  'host-1', ?, 'run-1', '2026-08-12 12:00:00+00', 'hash', '{}')",
        (event_id, account_id),
    )


def test_delete_account_transcripts_removes_only_that_account_and_records_the_fact() -> None:
    lake = build_fixture_analytics_session()
    ops = FakeOpsConnection()
    _insert_transcript_row(lake, "evt-1", "user-doomed")
    _insert_transcript_row(lake, "evt-2", "user-doomed")
    _insert_transcript_row(lake, "evt-3", "user-survivor")

    deleted_count = delete_account_transcripts(lake, ops, account_id="user-doomed", now=_NOW)

    assert deleted_count == 2
    assert count_account_transcript_rows(lake, "user-doomed") == 0
    assert count_account_transcript_rows(lake, "user-survivor") == 1
    statement, parameters = ops.recording_cursor.executed[-1]
    assert "INSERT INTO deletion_events" in statement
    assert parameters == ("user-doomed", _NOW, 2, "")

    # Idempotent: a re-run deletes nothing and records a zero-count fact.
    assert delete_account_transcripts(lake, ops, account_id="user-doomed", now=_NOW) == 0


def test_delete_account_transcripts_raises_before_recording_when_the_lake_is_broken() -> None:
    broken_lake = duckdb.connect()
    ops = FakeOpsConnection()

    with pytest.raises(DeletionError):
        delete_account_transcripts(broken_lake, ops, account_id="user-doomed", now=_NOW)

    assert ops.recording_cursor.executed == []

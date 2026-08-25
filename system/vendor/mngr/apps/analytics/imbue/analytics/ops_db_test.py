from datetime import datetime
from datetime import timezone

from imbue.analytics.mock_ops_db_test import FakeOpsConnection
from imbue.analytics.ops_db import read_consent_ledger
from imbue.analytics.ops_db import read_cursors_for_host
from imbue.analytics.ops_db import read_last_collection_attempts
from imbue.analytics.ops_db import record_collection_run
from imbue.analytics.ops_db import record_deletion_event
from imbue.analytics.ops_db import record_host_key
from imbue.analytics.ops_db import record_job_run
from imbue.analytics.ops_db import set_consent
from imbue.analytics.ops_db import write_cursor

_NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def test_record_job_run_inserts_one_row_with_every_field() -> None:
    connection = FakeOpsConnection()
    started_at = datetime(2026, 8, 18, 10, 0, tzinfo=timezone.utc)
    finished_at = datetime(2026, 8, 18, 10, 1, tzinfo=timezone.utc)

    record_job_run(
        connection,
        job_name="aggregation",
        started_at=started_at,
        finished_at=finished_at,
        is_success=True,
        detail="",
    )

    assert len(connection.recording_cursor.executed) == 1
    statement, parameters = connection.recording_cursor.executed[0]
    assert "INSERT INTO job_runs" in statement
    assert parameters == ("aggregation", started_at, finished_at, True, "")


def test_consent_ledger_roundtrip_shapes() -> None:
    connection = FakeOpsConnection()
    connection.recording_cursor.rows_to_return = [("user-a", True), ("user-b", False)]

    assert read_consent_ledger(connection) == {"user-a": True, "user-b": False}

    set_consent(connection, account_id="user-c", is_consenting=True, now=_NOW)
    statement, parameters = connection.recording_cursor.executed[-1]
    assert "INSERT INTO consent_ledger" in statement
    assert "ON CONFLICT (account_id)" in statement
    # first_consented_at is only set on insert; the upsert must not overwrite it.
    assert "first_consented_at = EXCLUDED" not in statement
    assert parameters == ("user-c", True, _NOW, _NOW)


def test_cursor_reads_and_upserts_are_keyed_by_host_and_source() -> None:
    connection = FakeOpsConnection()
    connection.recording_cursor.rows_to_return = [("transcripts", '{"agent-1": 42}')]

    assert read_cursors_for_host(connection, "host-1") == {"transcripts": '{"agent-1": 42}'}

    write_cursor(connection, host_id="host-1", source="transcripts", cursor_value='{"agent-1": 99}', now=_NOW)
    statement, parameters = connection.recording_cursor.executed[-1]
    assert "ON CONFLICT (host_id, source)" in statement
    assert parameters == ("host-1", "transcripts", '{"agent-1": 99}', _NOW)


def test_host_key_upsert_and_last_attempt_read() -> None:
    connection = FakeOpsConnection()

    record_host_key(connection, host_id="host-1", endpoint="container", host_public_key="ssh-ed25519 AAAA", now=_NOW)
    statement, parameters = connection.recording_cursor.executed[-1]
    assert "ON CONFLICT (host_id, endpoint)" in statement
    assert parameters == ("host-1", "container", "ssh-ed25519 AAAA", _NOW, _NOW)

    connection.recording_cursor.rows_to_return = [("host-1", _NOW)]
    assert read_last_collection_attempts(connection) == {"host-1": _NOW}


def test_record_collection_run_inserts_every_audit_field() -> None:
    connection = FakeOpsConnection()

    record_collection_run(
        connection,
        run_id="run-1",
        host_id="host-1",
        account_id="user-a",
        started_at=_NOW,
        finished_at=_NOW,
        outcome="ok",
        script_version="abc123",
        metrics_rows=10,
        transcript_rows=5,
        dropped_lines=1,
        stdout_bytes=2048,
        is_host_key_changed=False,
        detail="",
    )

    statement, parameters = connection.recording_cursor.executed[-1]
    assert "INSERT INTO collection_runs" in statement
    assert parameters == ("run-1", "host-1", "user-a", _NOW, _NOW, "ok", "abc123", 10, 5, 1, 2048, False, "")


def test_record_deletion_event_inserts_the_fact_row() -> None:
    connection = FakeOpsConnection()

    record_deletion_event(connection, account_id="user-a", requested_at=_NOW, transcript_rows_deleted=7, detail="")

    statement, parameters = connection.recording_cursor.executed[-1]
    assert "INSERT INTO deletion_events" in statement
    assert parameters == ("user-a", _NOW, 7, "")

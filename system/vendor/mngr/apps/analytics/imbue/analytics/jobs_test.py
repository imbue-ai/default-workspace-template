import logging
from datetime import datetime
from datetime import timezone

import pytest

from imbue.analytics.errors import AggregationError
from imbue.analytics.jobs import JobRunRecord
from imbue.analytics.jobs import record_run_row_in_ops_db
from imbue.analytics.jobs import run_recorded_job
from imbue.analytics.mock_ops_db_test import FakeOpsConnection


def test_run_recorded_job_records_a_success_row_and_returns_the_counters() -> None:
    recorded: list[JobRunRecord] = []

    counters = run_recorded_job(
        "aggregation",
        warn_seconds=60.0,
        job_body=lambda: {"activity_rows": 7},
        record_run=recorded.append,
    )

    assert counters == {"activity_rows": 7}
    assert len(recorded) == 1
    assert recorded[0].job_name == "aggregation"
    assert recorded[0].is_success is True
    assert recorded[0].detail == ""
    assert recorded[0].finished_at >= recorded[0].started_at


def _raise_aggregation_error() -> dict[str, int]:
    raise AggregationError("the lake is unreachable")


def test_run_recorded_job_records_a_failure_row_and_reraises() -> None:
    recorded: list[JobRunRecord] = []

    with pytest.raises(AggregationError, match="the lake is unreachable"):
        run_recorded_job(
            "aggregation",
            warn_seconds=60.0,
            job_body=_raise_aggregation_error,
            record_run=recorded.append,
        )

    assert len(recorded) == 1
    assert recorded[0].is_success is False
    assert recorded[0].detail == "the lake is unreachable"


def test_run_recorded_job_warns_when_the_duration_crosses_the_threshold(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="imbue.analytics.jobs"):
        run_recorded_job(
            "lake_maintenance",
            warn_seconds=0.0,
            job_body=lambda: {},
            record_run=lambda record: None,
        )

    assert any("approaching its time budget" in message for message in caplog.messages)


def test_record_run_row_in_ops_db_writes_the_row_and_closes_the_connection() -> None:
    record = JobRunRecord(
        job_name="aggregation",
        started_at=datetime(2026, 8, 18, 10, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 8, 18, 10, 1, tzinfo=timezone.utc),
        is_success=True,
        detail="",
    )
    connection = FakeOpsConnection()

    record_run_row_in_ops_db(lambda: connection, record)

    assert connection.is_closed is True
    assert len(connection.recording_cursor.executed) == 1
    statement, parameters = connection.recording_cursor.executed[0]
    assert "INSERT INTO job_runs" in statement
    assert parameters[0] == "aggregation"

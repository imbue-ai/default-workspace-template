import json
from datetime import datetime
from datetime import timezone
from pathlib import Path

from share_gateway.retry_state import RETRY_DELAYS_SECONDS
from share_gateway.retry_state import STATUS_RETRYING
from share_gateway.retry_state import ProvisioningRetryState
from share_gateway.retry_state import ShareStackStartError
from share_gateway.retry_state import delay_for_failed_attempt
from share_gateway.retry_state import remove_gateway_status
from share_gateway.retry_state import write_gateway_status


def _retryable(retry_after_seconds: int | None = None) -> ShareStackStartError:
    return ShareStackStartError("connector unreachable", is_retryable=True, retry_after_seconds=retry_after_seconds)


def test_delay_schedule_walks_the_table_then_holds_at_the_cap() -> None:
    delays = [delay_for_failed_attempt(count, None) for count in range(1, len(RETRY_DELAYS_SECONDS) + 3)]
    assert delays == [15, 30, 60, 120, 480, 900, 900, 900]


def test_connector_retry_after_wins_only_when_longer_than_the_schedule() -> None:
    assert delay_for_failed_attempt(1, 600) == 600
    assert delay_for_failed_attempt(6, 60) == 900


def test_failures_space_attempts_out_and_success_resets() -> None:
    state = ProvisioningRetryState()
    assert state.is_attempt_due(now=100.0) is True

    assert state.record_failure(_retryable(), now=100.0) == 15
    assert state.is_attempt_due(now=110.0) is False
    assert state.is_attempt_due(now=115.0) is True

    assert state.record_failure(_retryable(), now=115.0) == 30
    assert state.failed_attempt_count == 2
    assert state.is_attempt_due(now=144.0) is False
    assert state.is_attempt_due(now=145.0) is True

    state.reset()
    assert state.failed_attempt_count == 0
    assert state.is_attempt_due(now=145.0) is True


def test_permanent_refusal_halts_retries_until_reset() -> None:
    state = ProvisioningRetryState()
    refusal = ShareStackStartError("connector refused the CSR (400)", is_retryable=False, retry_after_seconds=None)

    assert state.record_failure(refusal, now=5.0) is None

    assert state.is_halted is True
    assert state.is_attempt_due(now=10_000.0) is False
    assert state.last_error == "connector refused the CSR (400)"
    state.reset()
    assert state.is_halted is False
    assert state.is_attempt_due(now=10_000.0) is True


def test_status_file_reports_the_next_retry_and_truncates_the_error(tmp_path: Path) -> None:
    status_path = tmp_path / "state" / "status.json"
    now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)

    write_gateway_status(
        status_path,
        state=STATUS_RETRYING,
        workspace_domain="abc.def.us1.example.com",
        failed_attempt_count=3,
        last_error="x" * 900,
        next_retry_in_seconds=60,
        now=now,
    )

    payload = json.loads(status_path.read_text())
    assert payload["state"] == "retrying"
    assert payload["workspace_domain"] == "abc.def.us1.example.com"
    assert payload["failed_attempt_count"] == 3
    assert len(payload["last_error"]) == 500
    assert payload["next_retry_at"] == "2026-09-13T12:01:00+00:00"
    assert payload["updated_at"] == "2026-09-13T12:00:00+00:00"
    assert not status_path.with_name("status.json.tmp").exists()

    remove_gateway_status(status_path)
    assert not status_path.exists()
    remove_gateway_status(status_path)

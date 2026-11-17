import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from screen.install_unit import SCREEN_INSTALL_UNIT
from screen.install_unit import UnitFailure
from screen.install_unit import describe_unit_failure
from screen.install_unit import unit_failure_since

_BOOT = datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)


def _event(event_type: str, at: datetime, unit: str = SCREEN_INSTALL_UNIT, **detail: object) -> str:
    """One line as env-converge's emit_event writes it."""
    return json.dumps(
        {
            "timestamp": at.isoformat(),
            "type": event_type,
            "event_id": "evc-1",
            "source": "env_converge",
            "detail": {"unit": unit, **detail},
        }
    )


def test_no_record_of_the_unit_is_no_failure() -> None:
    lines = [_event("unit_failed", _BOOT + timedelta(minutes=1), unit="1100-secret-scanners.sh", exit_code=1)]
    assert unit_failure_since(lines, SCREEN_INSTALL_UNIT, _BOOT) is None
    assert unit_failure_since([], SCREEN_INSTALL_UNIT, _BOOT) is None


def test_a_failure_in_this_boot_is_read_with_its_exit_code_and_error_output() -> None:
    lines = [
        "not json",
        _event("unit_failed", _BOOT + timedelta(minutes=2), exit_code=100, stderr_tail="E: Unable to locate package"),
    ]
    failure = unit_failure_since(lines, SCREEN_INSTALL_UNIT, _BOOT)
    assert failure == UnitFailure(exit_code=100, stderr_tail="E: Unable to locate package")
    described = describe_unit_failure(failure)
    assert "2000-screen.sh" in described and "exit code 100" in described
    assert described.endswith("E: Unable to locate package")


def test_a_failure_from_an_earlier_boot_is_ignored() -> None:
    lines = [_event("unit_failed", _BOOT - timedelta(hours=3), exit_code=1, stderr_tail="old")]
    assert unit_failure_since(lines, SCREEN_INSTALL_UNIT, _BOOT) is None


def test_a_later_successful_run_clears_the_failure() -> None:
    lines = [
        _event("unit_failed", _BOOT + timedelta(minutes=1), exit_code=1, stderr_tail="network down"),
        _event("unit_run", _BOOT + timedelta(minutes=9), duration_seconds=40.0),
    ]
    assert unit_failure_since(lines, SCREEN_INSTALL_UNIT, _BOOT) is None

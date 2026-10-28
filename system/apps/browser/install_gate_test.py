import json
from datetime import datetime, timedelta, timezone

from browser.install_gate import (
    FORTRESS_UNIT,
    InstallState,
    UnitFailure,
    missing_install_gate,
    unit_failure_since,
)
from browser.runner import _install_refusal

_BOOT = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)


def _event(event_type: str, unit: str, at: datetime, stderr_tail: str = "") -> str:
    detail = {"unit": unit, "exit_code": 1, "stderr_tail": stderr_tail}
    return json.dumps({"timestamp": at.isoformat(), "type": event_type, "detail": detail})


def test_a_fortress_unit_that_failed_in_this_boot_is_a_failed_install() -> None:
    lines = [
        _event("unit_run", FORTRESS_UNIT, _BOOT - timedelta(days=2)),
        _event("unit_failed", FORTRESS_UNIT, _BOOT + timedelta(minutes=4), "E: Unable to locate package xvfb"),
        _event("unit_run", "1100-secret-scanners.sh", _BOOT + timedelta(minutes=5)),
    ]

    assert unit_failure_since(lines, FORTRESS_UNIT, _BOOT) == UnitFailure(
        exit_code=1, stderr_tail="E: Unable to locate package xvfb"
    )


def test_a_failure_from_a_previous_boot_is_still_installing_until_this_boots_pass_reports() -> None:
    lines = [_event("unit_failed", FORTRESS_UNIT, _BOOT - timedelta(hours=3))]

    assert unit_failure_since(lines, FORTRESS_UNIT, _BOOT) is None


def test_a_later_successful_run_clears_a_failure() -> None:
    lines = [
        _event("unit_failed", FORTRESS_UNIT, _BOOT + timedelta(minutes=1)),
        _event("unit_run", FORTRESS_UNIT, _BOOT + timedelta(minutes=9)),
    ]

    assert unit_failure_since(lines, FORTRESS_UNIT, _BOOT) is None


def test_lines_that_are_not_its_events_are_skipped() -> None:
    lines = [
        "not json",
        json.dumps(["a", "list"]),
        json.dumps({"type": "unit_failed", "detail": "not a dict"}),
        json.dumps({"type": "unit_failed", "timestamp": "yesterday", "detail": {"unit": FORTRESS_UNIT}}),
        _event("unit_failed", "1100-secret-scanners.sh", _BOOT + timedelta(minutes=1)),
    ]

    assert unit_failure_since(lines, FORTRESS_UNIT, _BOOT) is None


def test_a_missing_engine_tells_installing_from_failed() -> None:
    installing = missing_install_gate("Chromium", failure=None)
    failed = missing_install_gate("Chromium", failure=UnitFailure(exit_code=100, stderr_tail="E: dpkg was interrupted"))

    assert (installing.state, installing.is_ready, installing.agent_fix) == (InstallState.INSTALLING, False, None)
    assert (failed.state, failed.is_ready) == (InstallState.FAILED, False)
    assert installing.user_facing_message != failed.user_facing_message


def test_a_failed_install_asks_an_agent_with_the_failed_unit_and_what_it_wrote() -> None:
    failed = missing_install_gate("Chromium", failure=UnitFailure(exit_code=100, stderr_tail="E: dpkg was interrupted"))

    body = _install_refusal(failed).model_dump(mode="json")

    assert body["detail"] == failed.reason
    ask_agent = body["ask_agent"]
    assert isinstance(ask_agent, dict)
    assert ask_agent["what_failed"] == "The browser couldn't be set up in this workspace"
    details = ask_agent["details"]
    assert f"system/scripts/env.d/{FORTRESS_UNIT}" in details
    assert "exit code 100" in details
    assert details.endswith("E: dpkg was interrupted")


def test_a_browser_still_installing_offers_no_agent() -> None:
    body = _install_refusal(missing_install_gate("Chromium", failure=None)).model_dump(mode="json")

    assert "ask_agent" not in body

"""The check the routine runs every 10 minutes: is the disk over the alert line, and has the user been told already?

Nothing here talks to the chat app: the helper is replaced with a recorder, so what these tests judge is when the
check decides to alert, and what it says about it.
"""

from pathlib import Path

import pytest

from disk_space import check, helper, scan, store
from disk_space.testing import GIB, a_disk, a_node, install_disk, save_measurement

NOW = 1_700_000_000.0


@pytest.fixture
def helper_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str | None]]:
    calls: list[tuple[str, str, str | None]] = []

    def fake_start(reason: str, trigger: str, item: str | None = None) -> dict:
        calls.append((reason, trigger, item))
        return {"chat_id": "chat-abc", "created": True, "detail": ""}

    monkeypatch.setattr(helper, "start", fake_start)
    return calls


@pytest.fixture
def measurement(monkeypatch: pytest.MonkeyPatch) -> dict:
    snapshot = save_measurement([a_node("root", None, "Whole disk")], taken_at=NOW)
    rescans: list[bool] = []

    def fake_scan_locked() -> dict | None:
        rescans.append(True)
        return snapshot

    monkeypatch.setattr(scan, "scan_locked", fake_scan_locked)
    return {"snapshot": snapshot, "rescans": rescans}


def test_a_disk_under_the_line_is_checked_without_alerting(
    monkeypatch: pytest.MonkeyPatch, helper_calls: list, measurement: dict
) -> None:
    install_disk(monkeypatch, a_disk(used=50 * GIB, free=50 * GIB, percent=50))
    result = check.run_check(now=NOW)
    assert result == {
        "summary": "The disk is 50% full; the alert line is 90%.",
        "alerted": False,
    }
    assert helper_calls == []
    assert store.read_watch()["last_check_at"] == NOW
    assert store.read_watch()["armed"] is True
    assert store.read_history() == []


def test_a_disk_over_the_line_measures_then_starts_the_helper(
    monkeypatch: pytest.MonkeyPatch, helper_calls: list, measurement: dict
) -> None:
    install_disk(monkeypatch, a_disk(used=93 * GIB, free=7 * GIB, percent=93))
    result = check.run_check(now=NOW)
    assert result["alerted"] is True
    assert result["chat_id"] == "chat-abc"
    reason, trigger, item = helper_calls[0]
    assert trigger == "check"
    assert item is None
    assert "93% full" in reason
    assert "the line is 90%" in reason
    assert "Tell the user with a notification" in reason
    watch = store.read_watch()
    assert watch["armed"] is False
    assert watch["last_alert_at"] == NOW
    entry = store.read_history()[0]
    assert entry["kind"] == "alert"
    assert entry["chat_id"] == "chat-abc"
    assert entry["disk"]["percent"] == 93


def test_the_helper_is_not_started_twice_while_the_disk_stays_over(
    monkeypatch: pytest.MonkeyPatch, helper_calls: list, measurement: dict
) -> None:
    install_disk(monkeypatch, a_disk(used=93 * GIB, free=7 * GIB, percent=93))
    check.run_check(now=NOW)
    result = check.run_check(now=NOW + 60 * 60)
    assert result["alerted"] is False
    assert "was already started" in result["summary"]
    assert len(helper_calls) == 1


def test_the_user_is_nudged_again_after_the_renotify_window(
    monkeypatch: pytest.MonkeyPatch, helper_calls: list, measurement: dict
) -> None:
    install_disk(monkeypatch, a_disk(used=93 * GIB, free=7 * GIB, percent=93))
    check.run_check(now=NOW)
    result = check.run_check(now=NOW + (store.DEFAULT_RENOTIFY_HOURS + 1) * 3600)
    assert result["alerted"] is True
    assert len(helper_calls) == 2


def test_the_alert_re_arms_once_the_disk_drops_back_under_the_line(
    monkeypatch: pytest.MonkeyPatch, helper_calls: list, measurement: dict
) -> None:
    install_disk(
        monkeypatch, a_disk(percent=93), a_disk(percent=80), a_disk(percent=93)
    )
    check.run_check(now=NOW)
    assert check.run_check(now=NOW + 600)["alerted"] is False
    assert len(helper_calls) == 1
    assert store.read_watch()["armed"] is True
    # Back over the line: told once more, without waiting out the renotify window.
    assert check.run_check(now=NOW + 1200)["alerted"] is True
    assert len(helper_calls) == 2


def test_the_alert_line_is_the_one_in_the_settings(
    monkeypatch: pytest.MonkeyPatch, helper_calls: list, measurement: dict
) -> None:
    store.write_settings(95)
    install_disk(monkeypatch, a_disk(percent=93))
    assert check.run_check(now=NOW)["alerted"] is False
    assert helper_calls == []


def test_a_stale_measurement_is_taken_again_before_the_helper_starts(
    monkeypatch: pytest.MonkeyPatch, helper_calls: list, measurement: dict
) -> None:
    install_disk(monkeypatch, a_disk(percent=93))
    check.run_check(now=NOW + check.FRESH_SNAPSHOT_SECONDS + 60)
    assert measurement["rescans"] == [True]


def test_a_fresh_measurement_is_used_as_it_stands(
    monkeypatch: pytest.MonkeyPatch, helper_calls: list, measurement: dict
) -> None:
    install_disk(monkeypatch, a_disk(percent=93))
    check.run_check(now=NOW + 60)
    assert measurement["rescans"] == []


def test_a_helper_that_could_not_start_is_reported_rather_than_hidden(
    monkeypatch: pytest.MonkeyPatch, helper_calls: list, measurement: dict
) -> None:
    install_disk(monkeypatch, a_disk(percent=93))
    monkeypatch.setattr(
        helper,
        "start",
        lambda reason, trigger, item=None: {
            "chat_id": "",
            "created": False,
            "detail": "no account is connected",
        },
    )
    result = check.run_check(now=NOW)
    assert result["alerted"] is True
    assert result["chat_id"] == ""
    assert "could not start: no account is connected" in result["summary"]
    # It still counts as having told the user, so the next check does not start another chat.
    assert store.read_watch()["armed"] is False


def test_the_routine_result_carries_the_summary_and_the_chat(
    monkeypatch: pytest.MonkeyPatch,
    helper_calls: list,
    measurement: dict,
    tmp_path: Path,
) -> None:
    install_disk(monkeypatch, a_disk(percent=93))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("ROUTINES_RUN_DIR", str(run_dir))
    result = check.run_check(now=NOW)
    check.write_routine_result(result)
    assert (run_dir / "result.json").exists()
    assert '"chat_id": "chat-abc"' in (run_dir / "result.json").read_text()


def test_a_check_outside_a_routine_writes_no_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ROUTINES_RUN_DIR", raising=False)
    check.write_routine_result({"summary": "The disk is fine."})
    assert list(tmp_path.iterdir()) == []

from datetime import datetime, timezone

from usage_collector.limits import limit_readings
from usage_collector.testing import NOW, model_call_payload

_WINDOWS = {
    "five_hour": {
        "used_percentage": 28,
        "resets_at": 1792719000,
        "label": "5h",
        "window_seconds": 18000,
    },
    "seven_day": {
        "used_percentage": 8,
        "resets_at": 1793196000,
        "label": "7d",
        "window_seconds": 604800,
    },
}


def test_a_snapshot_event_gives_a_reading_per_window() -> None:
    readings = limit_readings(
        {
            "source": "claude/usage",
            "timestamp": "2026-10-22T22:51:00.000000000Z",
            "rate_limits": _WINDOWS,
        }
    )

    assert [(r.source, r.window, r.used_percentage) for r in readings] == [
        ("claude", "five_hour", 28),
        ("claude", "seven_day", 8),
    ]
    assert readings[0].resets_at == datetime.fromtimestamp(1792719000, tz=timezone.utc)
    assert readings[0].observed_at == datetime(
        2026, 10, 22, 22, 51, tzinfo=timezone.utc
    )


def test_a_model_call_carrying_windows_is_read_by_its_harness() -> None:
    [reading, _] = limit_readings(
        model_call_payload(rate_limits=_WINDOWS, attribution={"harness": "codex"})
    )

    assert (reading.source, reading.observed_at) == ("codex", NOW)


def test_an_api_key_session_has_no_readings() -> None:
    assert (
        limit_readings(
            {
                "source": "claude/usage",
                "timestamp": "2026-10-22T22:51:00Z",
                "rate_limits": None,
            }
        )
        == []
    )
    assert limit_readings(model_call_payload()) == []
    assert limit_readings("not an event") == []

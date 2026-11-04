"""Tests for the sampler's files as the app reads them: the latest reading, the longer histories, and the busy
stretches in plain words."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from system_monitor import cpu

_NOW = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)


def _write_recent(path: Path, sampled_at: float) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "sampled_at": sampled_at,
                "interval_seconds": 5,
                "cores": 4,
                "cpu": {
                    "usage": 0.37,
                    "by_pid": {"4121": 0.236, "x": 1, "4188": "bad"},
                },
                "memory": {"total": 8, "available": 3, "used": 5},
                "samples": [
                    [1000, 0.3, 0.6],
                    [1005, 0.35, 0.62],
                    ["bad"],
                    [1010, None, 0.62],
                ],
            }
        )
    )


def test_the_latest_reading_is_read_while_fresh_and_dropped_once_stale(
    tmp_path: Path,
) -> None:
    path = tmp_path / "recent.json"
    _write_recent(path, sampled_at=1000.0)

    reading = cpu.read_recent(path, now=1020.0)

    assert reading is not None
    assert (reading.usage, reading.cores, reading.sampled_at) == (0.37, 4, 1000.0)
    assert reading.by_pid == {4121: 0.236}
    assert reading.samples == [
        {"t": 1000, "cpu": 0.3, "memory": 0.6},
        {"t": 1005, "cpu": 0.35, "memory": 0.62},
    ]
    assert cpu.read_recent(path, now=1031.0) is None
    assert cpu.read_recent(tmp_path / "missing.json", now=1000.0) is None


def test_the_history_is_averaged_into_the_span_s_steps_and_gaps_stay_gaps(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.jsonl"
    end_ms = 3_600_000 * 10
    lines = [
        {"t": end_ms - 3_540_000, "cpu": 0.2, "cpu_max": 0.5, "memory": 0.5},
        {"t": end_ms - 3_480_000, "cpu": 0.4, "cpu_max": 0.6, "memory": 0.5},
        # Twenty minutes with no lines, then a line, then one outside the span and a bad one.
        {"t": end_ms - 60_000, "cpu": 0.9, "cpu_max": 0.9, "memory": 0.7},
        {"t": end_ms - 3_700_000, "cpu": 0.1, "cpu_max": 0.1, "memory": 0.1},
        {"t": "bad", "cpu": 0.1, "memory": 0.1},
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines) + "not json\n")

    hour = cpu.read_history("1h", path, now_ms=end_ms)
    assert hour == [
        {"t": end_ms - 3_540_000, "cpu": 0.2, "memory": 0.5},
        {"t": end_ms - 3_480_000, "cpu": 0.4, "memory": 0.5},
        {"t": end_ms - 60_000, "cpu": 0.9, "memory": 0.7},
    ]
    # Over a day the points are five-minute means: the two lines a minute apart fall in one step, and the
    # line from just over an hour ago, outside the hour, is inside the day.
    day = cpu.read_history("24h", path, now_ms=end_ms)
    assert [point["cpu"] for point in day] == [0.1, 0.3, 0.9]
    assert cpu.read_history("1h", tmp_path / "missing.jsonl") == []


def test_stretches_of_the_last_day_are_named_and_worded_newest_first(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"

    def stretch(started: datetime, duration: int, top: list[dict[str, object]]) -> str:
        return json.dumps(
            {
                "timestamp": started.strftime("%Y-%m-%dT%H:%M:%S.%f000Z"),
                "type": "cpu_stretch",
                "event_id": "cpu-stretch-x",
                "source": "cpu_stretches",
                "started_at": started.strftime("%Y-%m-%dT%H:%M:%S.%f000Z"),
                "ended_at": started.strftime("%Y-%m-%dT%H:%M:%S.%f000Z"),
                "duration_seconds": duration,
                "peak": 0.98,
                "average": 0.93,
                "cores": 4,
                "top": top,
                "other_share": 0.1,
            }
        )

    path.write_text(
        "\n".join(
            [
                stretch(
                    _NOW - timedelta(hours=3),
                    504,
                    [
                        {"kind": "chat", "agent_id": "agent-a", "share": 0.71},
                        {"kind": "program", "program": "recipe-box", "share": 0.12},
                    ],
                ),
                stretch(_NOW - timedelta(minutes=20), 130, []),
                stretch(
                    _NOW - timedelta(hours=30),
                    300,
                    [{"kind": "chat", "agent_id": "agent-b", "share": 0.9}],
                ),
                json.dumps({"type": "other", "started_at": "x"}),
                "not json",
            ]
        )
        + "\n"
    )

    events = cpu.recent_stretches(
        path,
        _NOW,
        chat_names={"agent-a": "Website redesign"},
        app_names={"recipe-box": "Recipe box"},
    )

    assert [event["what"] for event in events] == [
        "CPU above 90% for 2 minutes",
        "CPU above 90% for 8 minutes (mostly the Website redesign chat)",
    ]
    assert events[1]["top"] == [
        {"name": "the Website redesign chat", "share": 0.71},
        {"name": "Recipe box", "share": 0.12},
    ]
    assert events[1]["duration_seconds"] == 504 and events[1]["kind"] == "busy"
    assert cpu.recent_stretches(tmp_path / "missing.jsonl", _NOW) == []


def test_a_stretch_with_an_unknown_chat_is_still_described() -> None:
    assert (
        cpu.describe_stretch(59, [{"name": "a chat", "share": 0.5}])
        == "CPU above 90% for 1 minute (mostly a chat)"
    )

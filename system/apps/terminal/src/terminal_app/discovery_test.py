import json
from pathlib import Path

from app_manifest.primitives import AppName, AppUrl

from terminal_app.discovery import (
    build_server_registered_event,
    write_server_registered_event,
)

_TERMINAL = AppName("terminal")
_URL = AppUrl("http://localhost:7681")


def test_server_registered_event_has_todays_shape_with_a_nanosecond_timestamp() -> None:
    event = build_server_registered_event(1_756_900_000_123_456_789, _TERMINAL, _URL)

    assert event.model_dump(mode="json") == {
        "timestamp": "2025-09-03T11:46:40.123456789Z",
        "type": "server_registered",
        "event_id": event.event_id,
        "source": "servers",
        "server": "terminal",
        "url": "http://localhost:7681",
    }
    assert event.event_id.startswith("evt-") and len(event.event_id) == len("evt-") + 32


def test_event_ids_differ_across_emissions_of_the_same_server() -> None:
    first = build_server_registered_event(1_756_900_000_000_000_000, _TERMINAL, _URL)
    second = build_server_registered_event(1_756_900_000_000_000_001, _TERMINAL, _URL)

    assert first.event_id != second.event_id
    assert first == build_server_registered_event(
        1_756_900_000_000_000_000, _TERMINAL, _URL
    )


def test_write_appends_one_line_per_start(tmp_path: Path) -> None:
    write_server_registered_event(tmp_path, _TERMINAL, _URL)
    write_server_registered_event(tmp_path, _TERMINAL, _URL)

    lines = (tmp_path / "events" / "servers" / "events.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert all(json.loads(line)["server"] == "terminal" for line in lines)
    assert all(
        set(json.loads(line))
        == {"timestamp", "type", "event_id", "source", "server", "url"}
        for line in lines
    )

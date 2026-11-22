"""Tests for the early announcement of a registered app on the services stream (``forward_port.py --announce``)."""

import json
import os
import subprocess
import sys
from pathlib import Path

from app_registration import announcement

_SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "forward_port.py"
_ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><path d="M2 2h12v12H2z"/></svg>'
_ROW: dict[str, object] = {
    "name": "chat",
    "url": "http://localhost:8010",
    "label": "chat-abcd1234",
    "icon": _ICON,
    "display_name": "Chat",
    "system": True,
}


def _events(events_path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in events_path.read_text().splitlines()]


def test_a_registration_event_carries_the_rows_fields_under_the_envelope() -> None:
    event = announcement.registration_event(
        _ROW, timestamp="2026-11-10T18:52:44.103364000Z", event_id="evt-0"
    )
    assert event == {
        "timestamp": "2026-11-10T18:52:44.103364000Z",
        "type": "service_registered",
        "event_id": "evt-0",
        "source": "services",
        "service": "chat",
        "url": "http://localhost:8010",
        "label": "chat-abcd1234",
        "icon": _ICON,
        "display_name": "Chat",
        "system": True,
        "internal": False,
    }


def test_a_row_without_icon_or_display_name_announces_them_empty() -> None:
    event = announcement.registration_event(
        {"name": "owner-exec", "url": "http://localhost:8080", "internal": True},
        timestamp="t",
        event_id="e",
    )
    assert (
        event["label"],
        event["icon"],
        event["display_name"],
        event["internal"],
    ) == (
        "",
        "",
        "",
        True,
    )


def test_the_timestamp_is_the_envelopes_nanosecond_utc_form() -> None:
    assert (
        announcement.nanosecond_timestamp(1_794_336_764_103_364_000)
        == "2026-11-10T18:52:44.103364000Z"
    )


def test_a_row_is_appended_once_and_not_again_while_it_is_unchanged(
    tmp_path: Path,
) -> None:
    events_path = tmp_path / "events" / "services" / "events.jsonl"
    assert announcement.announce_row(_ROW, events_path)
    assert not announcement.announce_row(_ROW, events_path)
    events = _events(events_path)
    assert len(events) == 1
    assert events[0]["service"] == "chat"
    assert str(events[0]["event_id"]).startswith("evt-")


def test_a_changed_row_is_announced_again(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    announcement.announce_row(_ROW, events_path)
    assert announcement.announce_row(
        {**_ROW, "url": "http://localhost:9010"}, events_path
    )
    assert [event["url"] for event in _events(events_path)] == [
        "http://localhost:8010",
        "http://localhost:9010",
    ]


def test_a_row_deregistered_since_its_last_announcement_is_announced_again(
    tmp_path: Path,
) -> None:
    events_path = tmp_path / "events.jsonl"
    announcement.announce_row(_ROW, events_path)
    with events_path.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "service_deregistered",
                    "source": "services",
                    "service": "chat",
                }
            )
            + "\n"
        )
    assert announcement.announce_row(_ROW, events_path)


def test_other_apps_and_unparseable_lines_do_not_hide_the_rows_latest_event(
    tmp_path: Path,
) -> None:
    events_path = tmp_path / "events.jsonl"
    announcement.announce_row(_ROW, events_path)
    with events_path.open("a") as stream:
        stream.write("not json\n")
        stream.write(
            json.dumps({"type": "service_registered", "service": "files"}) + "\n"
        )
    assert not announcement.announce_row(_ROW, events_path)


def test_a_stream_that_cannot_be_written_is_skipped_without_raising(
    tmp_path: Path,
) -> None:
    blocker = tmp_path / "events"
    blocker.write_text("a file where the stream's directory would go")
    assert not announcement.announce_row(_ROW, blocker / "services" / "events.jsonl")


def test_the_stream_lives_under_the_agent_state_directory(tmp_path: Path) -> None:
    assert (
        announcement.announcements_path({"MNGR_AGENT_STATE_DIR": str(tmp_path)})
        == tmp_path / "events" / "services" / "events.jsonl"
    )
    assert announcement.announcements_path({}) is None


def _run(
    args: list[str], apps_file: Path, state_dir: Path | None
) -> subprocess.CompletedProcess[str]:
    env = {
        key: value for key, value in os.environ.items() if key != "MNGR_AGENT_STATE_DIR"
    }
    env["MINDS_APPS_FILE"] = str(apps_file)
    if state_dir is not None:
        env["MNGR_AGENT_STATE_DIR"] = str(state_dir)
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_forward_port_announce_appends_the_registered_row(tmp_path: Path) -> None:
    (tmp_path / "icon.svg").write_text(_ICON)
    apps_file = tmp_path / "apps.toml"
    result = _run(
        [
            "--name",
            "files",
            "--url",
            "http://localhost:8300",
            "--icon-file",
            str(tmp_path / "icon.svg"),
            "--announce",
        ],
        apps_file,
        tmp_path / "agent",
    )
    assert result.returncode == 0, result.stderr
    (event,) = _events(tmp_path / "agent" / "events" / "services" / "events.jsonl")
    assert (event["service"], event["url"], event["icon"]) == (
        "files",
        "http://localhost:8300",
        _ICON,
    )
    assert str(event["label"]).startswith("files-")


def test_forward_port_announce_outside_a_workspace_only_registers(
    tmp_path: Path,
) -> None:
    apps_file = tmp_path / "apps.toml"
    result = _run(
        [
            "--name",
            "files",
            "--url",
            "http://localhost:8300",
            "--no-icon",
            "--announce",
        ],
        apps_file,
        None,
    )
    assert result.returncode == 0, result.stderr
    assert 'name = "files"' in apps_file.read_text()


def test_forward_port_refuses_announce_with_remove(tmp_path: Path) -> None:
    result = _run(
        ["--remove", "--name", "files", "--announce"], tmp_path / "apps.toml", tmp_path
    )
    assert result.returncode != 0
    assert "--announce cannot be combined with --remove" in result.stderr

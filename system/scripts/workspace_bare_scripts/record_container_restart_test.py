"""Tests for record_container_restart.py: the container start hook's record of an out-of-band restart."""

from __future__ import annotations

import io
import json
from pathlib import Path

from workspace_bare_scripts import record_container_restart


def test_the_restart_lands_as_one_enveloped_event_in_the_agents_host_lifecycle_stream(
    tmp_path: Path,
) -> None:
    agent_dir = tmp_path / "agent-0123456789abcdef0123456789abcdef"
    agent_dir.mkdir()
    uptime = tmp_path / "uptime"
    uptime.write_text("12.34 20.00\n")

    events_path = record_container_restart.record_container_restart(
        agent_dir, uptime, 1_794_184_007_123_456_789
    )
    record_container_restart.record_container_restart(
        agent_dir, uptime, 1_794_184_067_000_000_000
    )

    assert events_path == agent_dir / "events" / "host_lifecycle" / "events.jsonl"
    lines = [json.loads(line) for line in events_path.read_text().splitlines()]
    assert len(lines) == 2
    first = lines[0]
    assert first["type"] == "container_restarted"
    assert first["source"] == "host_lifecycle"
    assert first["timestamp"] == "2026-11-09T00:26:47.123456789Z"
    assert first["event_id"].startswith("evt-")
    assert first["event_id"] != lines[1]["event_id"]
    assert first["agent_id"] == "agent-0123456789abcdef0123456789abcdef"
    assert first["container_uptime_seconds"] == 12.34
    assert "restarted out of band" in first["message"]


def test_an_unreadable_uptime_is_recorded_as_unknown(tmp_path: Path) -> None:
    assert (
        record_container_restart.read_container_uptime_seconds(tmp_path / "missing")
        is None
    )
    garbage = tmp_path / "garbage"
    garbage.write_text("not a number\n")
    assert record_container_restart.read_container_uptime_seconds(garbage) is None


def test_main_refuses_a_missing_agent_directory(tmp_path: Path) -> None:
    stdout, stderr = io.StringIO(), io.StringIO()
    assert (
        record_container_restart.main(
            ["--agent-dir", str(tmp_path / "nope")], stdout, stderr
        )
        == 2
    )
    assert "not an agent directory" in stderr.getvalue()


def test_main_records_into_an_existing_agent_directory(tmp_path: Path) -> None:
    agent_dir = tmp_path / "agent-abcdef0123456789abcdef0123456789"
    agent_dir.mkdir()
    stdout, stderr = io.StringIO(), io.StringIO()
    assert (
        record_container_restart.main(["--agent-dir", str(agent_dir)], stdout, stderr)
        == 0
    )
    assert (agent_dir / "events" / "host_lifecycle" / "events.jsonl").exists()
    assert "recorded container_restarted" in stdout.getvalue()

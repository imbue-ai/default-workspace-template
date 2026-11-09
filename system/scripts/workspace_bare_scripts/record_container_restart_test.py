"""Tests for record_container_restart.py: the container start hook's record of an out-of-band restart."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from workspace_bare_scripts import record_container_restart

_CLOCK_TICKS_PER_SECOND = 100


def _write_proc(proc_root: Path, kernel_uptime: str, pid1_stat: str) -> Path:
    proc_root.mkdir()
    (proc_root / "uptime").write_text(kernel_uptime)
    (proc_root / "1").mkdir()
    (proc_root / "1" / "stat").write_text(pid1_stat)
    return proc_root


def _pid1_stat(start_ticks: int) -> str:
    """A /proc/1/stat line whose command name holds a space and a parenthesis, starting at ``start_ticks``."""
    fields_after_comm = ["S"] + ["0"] * 18 + [str(start_ticks), "0"]
    return "1 (sh (x) y) " + " ".join(fields_after_comm) + "\n"


def test_the_restart_lands_as_one_enveloped_event_in_the_agents_host_lifecycle_stream(
    tmp_path: Path,
) -> None:
    agent_dir = tmp_path / "agent-0123456789abcdef0123456789abcdef"
    agent_dir.mkdir()
    proc_root = _write_proc(tmp_path / "proc", "112.34 20.00\n", _pid1_stat(10_000))

    events_path = record_container_restart.record_container_restart(
        agent_dir, proc_root, _CLOCK_TICKS_PER_SECOND, 1_794_184_007_123_456_789
    )
    record_container_restart.record_container_restart(
        agent_dir, proc_root, _CLOCK_TICKS_PER_SECOND, 1_794_184_067_000_000_000
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
    assert "started with its services down" in first["message"]


def test_the_uptime_is_the_containers_even_when_the_kernel_has_been_up_far_longer(
    tmp_path: Path,
) -> None:
    # Under runc /proc/uptime is the host's: a day up, with the container's PID 1 started 30 s ago.
    proc_root = _write_proc(
        tmp_path / "proc", "86400.00 1.00\n", _pid1_stat((86400 - 30) * 100)
    )
    assert (
        record_container_restart.read_container_uptime_seconds(
            proc_root, _CLOCK_TICKS_PER_SECOND
        )
        == 30.0
    )


@pytest.mark.parametrize(
    ("kernel_uptime", "pid1_stat"),
    [
        ("not a number\n", _pid1_stat(100)),
        ("12.34 20.00\n", "1 (sh) S 0\n"),
        ("12.34 20.00\n", "no command name\n"),
    ],
)
def test_an_unreadable_uptime_is_recorded_as_unknown(
    tmp_path: Path, kernel_uptime: str, pid1_stat: str
) -> None:
    assert (
        record_container_restart.read_container_uptime_seconds(
            tmp_path / "missing", _CLOCK_TICKS_PER_SECOND
        )
        is None
    )
    proc_root = _write_proc(tmp_path / "proc", kernel_uptime, pid1_stat)
    assert (
        record_container_restart.read_container_uptime_seconds(
            proc_root, _CLOCK_TICKS_PER_SECOND
        )
        is None
    )


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

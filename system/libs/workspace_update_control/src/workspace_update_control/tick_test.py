"""The dispatcher's minute as a program: run in this process, by its entry file, and as the cron line runs it."""

import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from workspace_update_control import control, tick
from workspace_update_control.testing import (
    STAGED_CREATED_CHAT,
    StagedUpdateWorkspace,
    armed_schedule,
    listed_agent,
)

_LIBRARY_SOURCE = Path(__file__).resolve().parents[1]
_ENTRY_FILE = (
    Path(__file__).resolve().parents[5]
    / "system"
    / "scripts"
    / "update_dispatcher_tick.py"
)
_LEAD = STAGED_CREATED_CHAT["name"]
_ALL_DAY = {"window_start_minute_utc": 0, "window_length_minutes": 24 * 60}


def _stage_a_run_whose_lead_stopped(staged: StagedUpdateWorkspace) -> str:
    """A run with no verdict whose lead the watchdog brings back at any time of day; the lead's id."""
    lead_id = f"agent-{uuid4().hex}"
    staged.write_state("run.json", {"chat_agent_name": _LEAD, "started_at": 1.0})
    staged.set_agents([listed_agent(_LEAD, "STOPPED", lead_id)])
    return lead_id


def _revivals(staged: StagedUpdateWorkspace) -> list[list[str]]:
    return [
        call
        for call in staged.tool_calls("message_chat.py")
        if call[:1] != ["--create"]
    ]


def test_the_tick_runs_one_minute_of_the_dispatcher_at_the_time_it_is_run(
    staged: StagedUpdateWorkspace,
) -> None:
    """A window that is open all day is open whenever the test runs, so the minute dispatches."""
    staged.write_state("schedule.json", {**armed_schedule(1.0), **_ALL_DAY})
    before = time.time()

    tick.main()

    assert len(staged.chat_creates()) == 1
    schedule = staged.read_state("schedule.json")
    assert schedule is not None
    assert schedule["marked_chat"] == _LEAD
    assert before <= schedule["marked_at"] <= time.time()


def test_the_entry_file_runs_the_tick_with_no_venv_in_the_workspace_it_is_run_from(
    staged: StagedUpdateWorkspace,
) -> None:
    lead_id = _stage_a_run_whose_lead_stopped(staged)

    completed = subprocess.run(
        [sys.executable, "-S", "-s", str(_ENTRY_FILE)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        cwd=staged.root,
    )

    assert completed.returncode == 0, completed.stderr
    revivals = _revivals(staged)
    assert [revival[:2] for revival in revivals] == [[lead_id, "-m"]]
    assert "Restarts so far: 1." in revivals[0][2]


def test_the_cron_line_runs_the_tick_from_the_workspace_root_and_keeps_its_output_in_the_log(
    staged: StagedUpdateWorkspace, tmp_path: Path
) -> None:
    """Cron runs the line's command with no working directory of the workspace's: the line changes to its root."""
    shutil.copy(_ENTRY_FILE, staged.root / "system" / "scripts" / _ENTRY_FILE.name)
    library = staged.root / "system" / "libs" / "workspace_update_control"
    library.mkdir(parents=True)
    (library / "src").symlink_to(_LIBRARY_SOURCE, target_is_directory=True)
    lead_id = _stage_a_run_whose_lead_stopped(staged)
    assert control.install_cron_entry(str(staged.live_cron_dir)) is True
    schedule_line = (
        (staged.live_cron_dir / "update-self-dispatch").read_text().splitlines()[1]
    )
    command = shlex.split(schedule_line)[-1]
    assert shlex.split(schedule_line)[:10] == [
        *["*"] * 5,
        "root",
        "flock",
        "-n",
        "/run/update-self-dispatch.lock",
        "-c",
    ]

    completed = subprocess.run(
        ["sh", "-c", command],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        cwd=tmp_path,
    )

    log_lines = staged.dispatcher_log_lines()
    assert completed.returncode == 0, log_lines
    assert [revival[:2] for revival in _revivals(staged)] == [[lead_id, "-m"]]
    assert any(
        line.endswith(f"restarted stopped lead {_LEAD} (restart 1)")
        for line in log_lines
    )

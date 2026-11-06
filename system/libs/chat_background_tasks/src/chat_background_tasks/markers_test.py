import json
import os
from pathlib import Path

import pytest

from chat_background_tasks import markers
from chat_background_tasks.markers import BackgroundTask, TaskSource
from chat_background_tasks.testing import runner_task

_CHAT_ID = "agent-0123456789abcdef0123456789abcdef"
_OTHER_CHAT_ID = "agent-fedcba9876543210fedcba9876543210"


def test_only_live_well_formed_markers_count_oldest_first(
    marker_root: Path, exited_pid: int
) -> None:
    markers.write_marker(
        marker_root, _CHAT_ID, runner_task("newer", "2026-10-08T10:05:00+00:00")
    )
    markers.write_marker(
        marker_root, _CHAT_ID, runner_task("older", "2026-10-08T10:00:00+00:00")
    )
    markers.write_marker(
        marker_root,
        _CHAT_ID,
        runner_task("stale", "2026-10-08T09:00:00+00:00", pid=exited_pid),
    )
    chat_dir = markers.chat_dir(marker_root, _CHAT_ID)
    (chat_dir / "run_in_background-garbled.json").write_text("{not json")
    (chat_dir / "run_in_background-nopid.json").write_text(
        json.dumps(
            {
                "source": "run_in_background",
                "id": "nopid",
                "description": "",
                "started_at": "",
            }
        )
    )
    (chat_dir / "harness--noharness.json").write_text(
        json.dumps(
            {
                "source": "harness",
                "id": "noharness",
                "description": "",
                "started_at": "",
                "pid": os.getpid(),
            }
        )
    )

    live = markers.list_live_tasks(marker_root, _CHAT_ID)

    assert [task.id for task in live] == ["older", "newer"]
    assert markers.is_busy(marker_root, _CHAT_ID)
    assert not markers.is_busy(marker_root, _OTHER_CHAT_ID)
    # A reader never deletes a stale marker; its writer does.
    assert (
        chat_dir / markers.marker_file_name(TaskSource.RUN_IN_BACKGROUND, "stale")
    ).exists()


def test_a_chat_whose_only_marker_is_stale_is_not_busy_and_not_listed(
    marker_root: Path, exited_pid: int
) -> None:
    markers.write_marker(
        marker_root,
        _CHAT_ID,
        runner_task("stale", "2026-10-08T09:00:00+00:00", pid=exited_pid),
    )
    markers.write_marker(
        marker_root, _OTHER_CHAT_ID, runner_task("live", "2026-10-08T09:00:00+00:00")
    )

    assert not markers.is_busy(marker_root, _CHAT_ID)
    assert list(markers.live_tasks_by_chat(marker_root)) == [_OTHER_CHAT_ID]


def test_a_rewritten_marker_replaces_the_old_one_and_leaves_no_temp_file(
    marker_root: Path,
) -> None:
    markers.write_marker(
        marker_root, _CHAT_ID, runner_task("one", "2026-10-08T10:00:00+00:00")
    )
    replacement = runner_task("one", "2026-10-08T10:00:00+00:00")._replace(
        description="Renamed"
    )
    markers.write_marker(marker_root, _CHAT_ID, replacement)

    assert markers.list_live_tasks(marker_root, _CHAT_ID) == [replacement]
    assert [
        path.name for path in markers.chat_dir(marker_root, _CHAT_ID).iterdir()
    ] == ["run_in_background-one.json"]


def test_a_harness_task_round_trips_under_its_harness_name(marker_root: Path) -> None:
    task = BackgroundTask(
        source=TaskSource.HARNESS,
        id="shell/1",
        description="tail logs",
        started_at="2026-10-08T10:00:00+00:00",
        pid=os.getpid(),
        harness="claude",
        kind="shell",
        command="tail -f log",
    )

    markers.write_marker(marker_root, _CHAT_ID, task)

    assert markers.list_live_tasks(marker_root, _CHAT_ID) == [task]
    assert [
        path.name for path in markers.chat_dir(marker_root, _CHAT_ID).iterdir()
    ] == ["harness-claude-shell_1.json"]
    markers.remove_marker(
        marker_root, _CHAT_ID, TaskSource.HARNESS, "shell/1", "claude"
    )
    assert not markers.is_busy(marker_root, _CHAT_ID)


@pytest.mark.skipif(
    not Path("/proc/self/stat").exists(),
    reason="needs Linux's /proc/<pid>/stat for a process's start time",
)
def test_a_marker_whose_pid_now_belongs_to_another_process_is_stale(
    marker_root: Path,
) -> None:
    """After a restart pids are handed out again; a marker naming a recycled pid must not count."""
    own_start = markers.process_start_time(os.getpid())
    for task_id, pid_start in (
        ("same", own_start),
        ("recycled", str(int(own_start) + 1)),
    ):
        markers.write_marker(
            marker_root,
            _CHAT_ID,
            runner_task(task_id, "2026-10-08T10:00:00+00:00")._replace(
                pid_start=pid_start
            ),
        )

    assert [task.id for task in markers.list_live_tasks(marker_root, _CHAT_ID)] == [
        "same"
    ]


def test_a_marker_whose_pid_no_process_can_have_is_stale(marker_root: Path) -> None:
    markers.write_marker(
        marker_root,
        _CHAT_ID,
        runner_task("overflow", "2026-10-08T10:00:00+00:00", pid=2**63),
    )

    assert markers.list_live_tasks(marker_root, _CHAT_ID) == []

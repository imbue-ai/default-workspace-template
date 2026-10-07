import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

from imbue.chat.background_tasks import background_tasks_dir
from imbue.chat.background_tasks import move_run_in_background_markers
from imbue.chat.background_tasks import read_live_background_tasks
from imbue.mngr.primitives import BackgroundTaskKind
from imbue.mngr.primitives import BackgroundTaskSource


def _dead_pid() -> int:
    finished = subprocess.Popen(["true"])
    finished.wait()
    return finished.pid


def _write_marker(marker_dir: Path, source: str, task_id: str, started_at: str, pid: int, **extra: str) -> Path:
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker_path = marker_dir / f"{source}-{task_id}.json"
    marker_path.write_text(
        json.dumps(
            {"source": source, "id": task_id, "description": f"task {task_id}", "started_at": started_at, "pid": pid}
            | extra
        )
    )
    return marker_path


def test_only_markers_whose_process_lives_are_read_and_oldest_comes_first(tmp_path: Path) -> None:
    marker_dir = tmp_path / "background_tasks"
    live_pid = os.getpid()
    _write_marker(marker_dir, "run_in_background", "newer", "2026-10-06T12:05:00Z", live_pid)
    _write_marker(marker_dir, "claude", "older", "2026-10-06T12:00:00Z", live_pid, kind="shell", command="sleep 9")
    _write_marker(marker_dir, "run_in_background", "stale", "2026-10-06T11:00:00Z", _dead_pid())
    (marker_dir / "run_in_background-broken.json").write_text("{not json")

    tasks = read_live_background_tasks(marker_dir)

    assert [(task.id, task.source, task.kind) for task in tasks] == [
        ("older", BackgroundTaskSource.CLAUDE, BackgroundTaskKind.SHELL),
        ("newer", BackgroundTaskSource.RUN_IN_BACKGROUND, None),
    ]
    # Readers never delete a stale marker; its writer does.
    assert (marker_dir / "run_in_background-stale.json").exists()


def test_a_missing_marker_dir_reads_as_no_tasks(tmp_path: Path) -> None:
    assert read_live_background_tasks(tmp_path / "background_tasks") == ()


def test_a_handoff_moves_only_the_runner_markers_into_the_successors_dir(tmp_path: Path) -> None:
    retiring = f"agent-{uuid4().hex}"
    successor = f"agent-{uuid4().hex}"
    retiring_dir = background_tasks_dir(tmp_path, retiring)
    runner_marker = _write_marker(retiring_dir, "run_in_background", "wait-1", "2026-10-06T12:00:00Z", os.getpid())
    claude_marker = _write_marker(retiring_dir, "claude", "bash-1", "2026-10-06T12:00:00Z", os.getpid(), kind="shell")

    move_run_in_background_markers(tmp_path, retiring, successor)

    successor_dir = background_tasks_dir(tmp_path, successor)
    assert not runner_marker.exists()
    assert claude_marker.exists()
    assert [task.id for task in read_live_background_tasks(successor_dir)] == ["wait-1"]
    assert [task.id for task in read_live_background_tasks(retiring_dir)] == ["bash-1"]


def test_a_handoff_with_nothing_pending_creates_nothing(tmp_path: Path) -> None:
    successor = f"agent-{uuid4().hex}"

    move_run_in_background_markers(tmp_path, f"agent-{uuid4().hex}", successor)

    assert not background_tasks_dir(tmp_path, successor).exists()

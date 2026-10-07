import json
import os
from datetime import datetime
from datetime import timezone
from pathlib import Path
from uuid import uuid4

from imbue.chat.background_tasks import background_tasks_dir
from imbue.chat.background_tasks import move_run_in_background_markers
from imbue.mngr.hosts.common import read_live_background_tasks_in_local_dir

# Markers written now: their recorder (this test process) started before them.
_NOW = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def test_a_handoff_moves_only_the_runner_markers_into_the_successors_dir(tmp_path: Path) -> None:
    retiring = f"agent-{uuid4().hex}"
    successor = f"agent-{uuid4().hex}"
    retiring_dir = background_tasks_dir(tmp_path, retiring)
    runner_marker = _write_marker(retiring_dir, "run_in_background", "wait-1", _NOW, os.getpid())
    claude_marker = _write_marker(retiring_dir, "claude", "bash-1", _NOW, os.getpid(), kind="shell")

    move_run_in_background_markers(tmp_path, retiring, successor)

    successor_dir = background_tasks_dir(tmp_path, successor)
    assert not runner_marker.exists()
    assert claude_marker.exists()
    assert [task.id for task in read_live_background_tasks_in_local_dir(successor_dir)] == ["wait-1"]
    assert [task.id for task in read_live_background_tasks_in_local_dir(retiring_dir)] == ["bash-1"]


def test_a_handoff_with_nothing_pending_creates_nothing(tmp_path: Path) -> None:
    successor = f"agent-{uuid4().hex}"

    move_run_in_background_markers(tmp_path, f"agent-{uuid4().hex}", successor)

    assert not background_tasks_dir(tmp_path, successor).exists()

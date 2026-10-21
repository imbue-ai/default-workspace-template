"""Shared helpers for the tests of the background-task markers and of their readers and writers."""

import os

from chat_background_tasks.markers import BackgroundTask, TaskSource


def runner_task(
    task_id: str, started_at: str, pid: int | None = None
) -> BackgroundTask:
    """A ``run_in_background`` task, kept live by this test process unless ``pid`` names another."""
    return BackgroundTask(
        source=TaskSource.RUN_IN_BACKGROUND,
        id=task_id,
        description=f"Task {task_id}",
        started_at=started_at,
        pid=os.getpid() if pid is None else pid,
    )

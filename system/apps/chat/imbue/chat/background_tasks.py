"""The chat's own read of an agent's background-task markers, and the handoff's move of them.

mngr records each pending task that will wake an agent as one JSON file in the agent's
``background_tasks`` dir and reports the live ones on every observe event. The chat reads the
same dir itself, the way it reads the ``active`` marker, because the observe stream re-probes
an agent only when its host shows activity: the flip when a report lands should not wait for
that. Parsing is mngr's own (``parse_background_task_marker``), so a marker means the same thing
here as in ``mngr list``; a marker whose pid is dead is stale and skipped, and nothing here
deletes one, since its writer's next pass does.
"""

import os
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.chat.agent_discovery import agent_state_dir
from imbue.mngr.hosts.common import BACKGROUND_TASKS_DIR_NAME
from imbue.mngr.hosts.common import parse_background_task_marker
from imbue.mngr.interfaces.data_types import BackgroundTask
from imbue.mngr.primitives import BackgroundTaskSource

# A marker file is ``<source>-<task id>.json``, the source spelled lower case on disk.
_MARKER_SUFFIX: Final[str] = ".json"
RUN_IN_BACKGROUND_MARKER_PREFIX: Final[str] = f"{BackgroundTaskSource.RUN_IN_BACKGROUND.value.lower()}-"


def background_tasks_dir(host_dir: Path, agent_id: str) -> Path:
    """The agent's marker dir under a host dir. Its mtime changes whenever a marker is added or removed."""
    return agent_state_dir(host_dir, agent_id) / BACKGROUND_TASKS_DIR_NAME


def is_process_alive(pid: int) -> bool:
    """Whether a process with this pid exists (one owned by another user counts)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_live_background_tasks(marker_dir: Path) -> tuple[BackgroundTask, ...]:
    """The tasks recorded in a marker dir whose recording process is alive, oldest first.

    A missing dir, a marker removed between the listing and its read, and a malformed marker
    all read as no task.
    """
    live_tasks: list[BackgroundTask] = []
    for marker_path in marker_dir.glob(f"*{_MARKER_SUFFIX}"):
        try:
            marker_json = marker_path.read_text()
        except OSError as e:
            logger.trace("Background-task marker {} went away before it was read: {}", marker_path, e)
            continue
        task = parse_background_task_marker(marker_json)
        if task is not None and is_process_alive(task.pid):
            live_tasks.append(task)
    # mngr's order (``select_live_background_tasks``): oldest first, the id breaking ties.
    return tuple(sorted(live_tasks, key=lambda task: (task.started_at, task.id)))


def move_run_in_background_markers(host_dir: Path, from_agent_id: str, to_agent_id: str) -> None:
    """Move one agent's ``run_in_background`` markers into another agent's marker dir.

    A handoff's successor inherits the retiring agent's pending commands: their reports are
    held for the chat and land on the successor, so the wait is now the successor's. The
    runner deletes its marker by task id under every agent dir once it delivers, so a moved
    marker is cleaned up wherever it went. Claude's own markers stay behind: their processes
    die with the retiring agent. A marker the runner deletes mid-move was delivered already;
    one that cannot be moved is logged, since a switch must not fail over a status marker.
    """
    from_dir = background_tasks_dir(host_dir, from_agent_id)
    to_dir = background_tasks_dir(host_dir, to_agent_id)
    marker_paths = sorted(from_dir.glob(f"{RUN_IN_BACKGROUND_MARKER_PREFIX}*{_MARKER_SUFFIX}"))
    if not marker_paths:
        return
    try:
        to_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("Could not create {} to move {} background-task markers into: {}", to_dir, len(marker_paths), e)
        return
    for marker_path in marker_paths:
        try:
            os.replace(marker_path, to_dir / marker_path.name)
        except FileNotFoundError:
            logger.debug("Background-task marker {} was removed before the handoff moved it", marker_path)
        except OSError as e:
            logger.warning("Could not move background-task marker {} to {}: {}", marker_path, to_dir, e)

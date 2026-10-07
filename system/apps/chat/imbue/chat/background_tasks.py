"""Where an agent's background-task markers live, and the handoff's move of them.

mngr records each pending task that will wake an agent as one JSON file in the agent's
``background_tasks`` dir and reports the live ones on every observe event. The chat also reads
the dir itself (with mngr's own ``read_live_background_tasks_in_local_dir``), the way it reads
the ``active`` marker, because the observe stream re-probes an agent only when its host shows
activity: the flip when a report lands should not wait for that.
"""

import os
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.chat.agent_discovery import agent_state_dir
from imbue.mngr.hosts.common import BACKGROUND_TASKS_DIR_NAME
from imbue.mngr.primitives import BackgroundTaskSource

# A marker file is ``<source>-<task id>.json``, the source spelled lower case on disk.
_MARKER_SUFFIX: Final[str] = ".json"
RUN_IN_BACKGROUND_MARKER_PREFIX: Final[str] = f"{BackgroundTaskSource.RUN_IN_BACKGROUND.value.lower()}-"


def background_tasks_dir(host_dir: Path, agent_id: str) -> Path:
    """The agent's marker dir under a host dir. Its mtime changes whenever a marker is added or removed."""
    return agent_state_dir(host_dir, agent_id) / BACKGROUND_TASKS_DIR_NAME


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

"""The background tasks each chat waits on, read through ``system/scripts/background_tasks.py``.

A chat is busy when its agent will resume on its own: its active agent has a turn in flight, or
its chat has a pending background task, whose completion starts one. Only the chat app knows the
first (the script's own file fallback, used when the app cannot answer, sees the tasks alone).
The pending tasks are one marker file each under ``<chat data dir>/background_tasks/<chat-id>/``,
written by ``run_in_background.py`` and by Claude's Stop hook. The script is their one reader: it is
standard library only (the hooks and skills run it with a bare ``python3``) and lives outside
this package, so the chat app loads it by path, as the module it is rather than through
``sys.path``, whose ``system/scripts`` holds generic module names, and wraps what it reads in
this app's types.
"""

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Final
from typing import Self

from loguru import logger
from pydantic import Field

from imbue.chat.errors import ChatAppError
from imbue.chat.primitives import ChatId
from imbue.imbue_common.frozen_model import FrozenModel

# Relative to the repo root the chat app runs from, as every path the app reads is.
BACKGROUND_TASKS_SCRIPT_PATH: Final[Path] = Path("system/scripts/background_tasks.py")
# The markers' root under the chat's data dir; the script's own default and the agents'
# ``MINDS_BACKGROUND_TASKS_DIR`` name the same directory.
BACKGROUND_TASKS_DIRNAME: Final[str] = "background_tasks"


class BackgroundTasksScriptError(ChatAppError):
    """The background-tasks script could not be loaded."""


class BackgroundTask(FrozenModel):
    """One pending task: a live marker, in the shape the script writes it (``BackgroundTask.to_json``)."""

    source: str = Field(description="Who wrote the marker: run_in_background, or claude (its Stop hook)")
    id: str = Field(description="The task's id within its source")
    description: str = Field(description="What the task is, as its writer described it")
    started_at: str = Field(
        description="When the task started (ISO 8601, UTC); for a Claude task, when its Stop hook first recorded "
        "it, at the end of the turn that started it"
    )
    pid: int = Field(description="The process whose exit makes the marker stale")
    kind: str = Field(default="", description="Claude's task type; '' otherwise")
    command: str = Field(default="", description="The command a Claude task runs, when it has one")
    pid_start: str = Field(
        default="", description="The pid's process start time, so a recycled pid cannot keep the marker live"
    )


def load_background_tasks_script(script_path: Path) -> ModuleType:
    """The script at ``script_path``, loaded as a module of its own. Raises ``BackgroundTasksScriptError``."""
    spec = importlib.util.spec_from_file_location("chat_background_tasks_script", script_path)
    if spec is None or spec.loader is None:
        raise BackgroundTasksScriptError(f"Cannot load the background-tasks script at {script_path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except OSError as e:
        raise BackgroundTasksScriptError(f"Cannot load the background-tasks script at {script_path}: {e}") from e
    return module


class BackgroundTaskReader(FrozenModel):
    """Reads one chat's live tasks from the marker root, through the script."""

    model_config = {"arbitrary_types_allowed": True}

    root: Path = Field(description="The marker root: one directory per chat beneath it")
    script: ModuleType = Field(description="The loaded background-tasks script")

    @classmethod
    def load(cls, root: Path, script_path: Path = BACKGROUND_TASKS_SCRIPT_PATH) -> Self:
        return cls(root=root, script=load_background_tasks_script(script_path))

    def chat_dir(self, chat_id: ChatId) -> Path:
        """The directory a chat's markers are written to, which need not exist."""
        return Path(self.script.chat_dir(self.root, str(chat_id)))

    def live_tasks(self, chat_id: ChatId) -> tuple[BackgroundTask, ...]:
        """The chat's tasks whose pid is alive, oldest first; malformed and stale markers are skipped."""
        return tuple(
            BackgroundTask(
                source=task.source,
                id=task.id,
                description=task.description,
                started_at=task.started_at,
                pid=task.pid,
                kind=task.kind,
                command=task.command,
                pid_start=task.pid_start,
            )
            for task in self.script.live_tasks_in(self.chat_dir(chat_id))
        )


def load_background_task_reader(
    root: Path, script_path: Path = BACKGROUND_TASKS_SCRIPT_PATH
) -> BackgroundTaskReader | None:
    """A reader over ``root``, or None when the script cannot be loaded (a boot outside a workspace, such as the
    update's pre-flight), in which case no chat reads as busy on a background task."""
    try:
        return BackgroundTaskReader.load(root, script_path)
    except BackgroundTasksScriptError as e:
        logger.warning("Reading no background tasks: {}", e)
        return None

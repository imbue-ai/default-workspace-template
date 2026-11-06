"""The background tasks each chat waits on, read from the markers ``chat_background_tasks`` owns.

A chat is busy when its agent will resume on its own: its active agent has a turn in flight, or
its chat has a pending background task, whose completion starts one. Only the chat app knows the
first (the ``system/scripts/background_tasks.py`` CLI's file fallback, used when the app cannot
answer, sees the tasks alone). The pending tasks are one marker file each under
``<chat data dir>/background_tasks/<chat-id>/``, written by ``run_in_background.py`` and by a
harness's hooks. This module wraps what the library reads in this app's types.
"""

from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

from chat_background_tasks import markers
from chat_background_tasks.markers import TaskSource
from loguru import logger
from pydantic import Field

from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.message_display import BACKGROUND_TASK_REPORT_TAG
from imbue.chat.primitives import ChatId
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

# The markers' root under the chat's data dir; the library's default and the agents'
# ``CHAT_BACKGROUND_TASKS_DIR`` name the same directory.
BACKGROUND_TASKS_DIRNAME: Final[str] = "background_tasks"


class BackgroundTask(FrozenModel):
    """One pending task: a live marker, in the shape the library writes it."""

    source: TaskSource = Field(
        description="Who recorded it: run_in_background (a command whose runner outlives the agent and reports how "
        "it ended to the chat), or harness (one of the harness's own tasks, which ends with the agent's process, "
        "unreported)"
    )
    id: str = Field(description="The task's id within its source")
    description: str = Field(description="What the task is, as its writer described it")
    started_at: str = Field(
        description="When the task started (ISO 8601, UTC); for a harness task, when its turn-end hook first "
        "recorded it, at the end of the turn that started it"
    )
    pid: int = Field(description="The process whose exit makes the marker stale")
    harness: str = Field(default="", description="The harness that recorded a harness task; '' otherwise")
    kind: str = Field(default="", description="The harness's type for a harness task; '' otherwise")
    command: str = Field(default="", description="The command the task runs, when its writer recorded one")
    pid_start: str = Field(
        default="", description="The pid's process start time, so a recycled pid cannot keep the marker live"
    )


class BackgroundTaskReader(FrozenModel):
    """Reads one chat's live tasks from the marker root."""

    root: Path = Field(description="The marker root: one directory per chat beneath it")

    def chat_dir(self, chat_id: ChatId) -> Path:
        """The directory a chat's markers are written to, which need not exist."""
        return markers.chat_dir(self.root, str(chat_id))

    def live_tasks(self, chat_id: ChatId) -> tuple[BackgroundTask, ...]:
        """The chat's tasks whose pid is alive, oldest first; malformed and stale markers are skipped."""
        return tuple(BackgroundTask(**task._asdict()) for task in markers.live_tasks_in(self.chat_dir(chat_id)))

    def remove_harness_tasks(self, chat_id: ChatId, harness: HarnessType, task_ids: tuple[str, ...]) -> None:
        """Remove the chat's markers for these tasks of the harness, which it has reported as over."""
        for task_id in task_ids:
            markers.remove_marker(self.root, str(chat_id), TaskSource.HARNESS, task_id, harness.value)

    def claim_unreported_runner_ends(self, chat_id: ChatId) -> tuple[BackgroundTask, ...]:
        """Take out the chat's ``run_in_background.py`` markers whose runner died before it reported, oldest first.

        Nothing else will say how those commands ended: the runner was stopped with them (Imbue Studio ending a
        chat's commands, a restart) or without them. Each marker goes to the one caller whose removal took it, so
        two sends at once cannot both report it.
        """
        directory = self.chat_dir(chat_id)
        claimed: list[BackgroundTask] = []
        for task in markers.read_markers(directory):
            if task.source is not TaskSource.RUN_IN_BACKGROUND or markers.is_task_live(task):
                continue
            try:
                (directory / task.file_name).unlink()
            except FileNotFoundError:
                continue
            claimed.append(BackgroundTask(**task._asdict()))
        return tuple(sorted(claimed, key=lambda task: task.started_at))

    def create_root(self) -> None:
        """Create the marker root, so the shell's avatar, which watches it only once it exists, sees the first task
        as it lands."""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("Could not create the background task marker root {}: {}", self.root, e)


@pure
def started_at_text(started_at: str) -> str:
    """When a task started, to the minute in UTC; the recorded text itself when it is not a timestamp."""
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return started.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


@pure
def command_as_inline_code(command: str) -> str:
    """A task's command as markdown inline code; as written when a backtick in it would end the code span."""
    return f"`{command}`" if "`" not in command else command


@pure
def unreported_runner_ends_notice(tasks: tuple[BackgroundTask, ...]) -> str:
    """The message telling a chat's agent which of its ``run_in_background.py`` commands will never report.

    A background-task report, so the chat shows it as a one-line notice and the agent reads the rest.
    """
    if len(tasks) == 1:
        summary = f"{' '.join(tasks[0].description.split()) or tasks[0].id} (ended without a report)"
    else:
        summary = f"{len(tasks)} background commands ended without a report"
    lines = [
        f"<{BACKGROUND_TASK_REPORT_TAG}>",
        f"<summary>{summary}</summary>",
        "The runner of each of these commands, which you started with run_in_background.py, was stopped before it "
        "could report how its command ended, so no report will come. The command was most likely stopped with it. "
        "Check whether you still need each one, and start it again if you do:",
    ]
    for task in tasks:
        command = f": {command_as_inline_code(task.command)}" if task.command else ""
        lines.append(f"- {task.description or task.id}{command} (started {started_at_text(task.started_at)})")
    lines.append(f"</{BACKGROUND_TASK_REPORT_TAG}>")
    return "\n".join(lines)

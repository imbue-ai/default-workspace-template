"""The marker files that record the background tasks a chat waits on.

A chat is busy when its agent will resume on its own: a turn is in flight, or a pending task's
completion will start one. A permission prompt is not busy, and neither is a process that will
never wake the agent. The pending tasks are one JSON file each, under the marker root::

    data/.apps/chat/background_tasks/<chat-id>/<writer>-<task-id>.json

keyed by chat, not agent, so a handoff moves nothing: the successor reads the same directory.
Each writer owns its own files and never reads another's:

- ``run_in_background.py``'s detached runner (``run_in_background-*``), which outlives the agent that
  started it and reports how its command ended to the chat, even when stopping the agent ended it;
- a harness's hooks (``harness-<harness>-*``), recording the harness's own background tasks, which
  end with the agent's process and never report.
  ``system/scripts/claude_background_task_hooks.py`` is Claude's.

Each marker names the pid whose death makes it stale (the runner, or the harness's main process),
and where ``/proc`` has it, that process's start time, so a pid recycled after a restart does not
bring a marker back. Readers skip a stale marker and never delete it; its writer's next pass does,
and the chat app takes out a dead runner's to tell the agent its command will never report.

The marker root is ``data/.apps/chat/background_tasks`` under the workspace's main checkout: the
repo the caller names (by default the one this package is in), or, when that is a git worktree (a
worker's work dir), the checkout the worktree belongs to, whose ``data/`` is the one the chat app
reads. ``$CHAT_BACKGROUND_TASKS_DIR`` overrides it.
"""

import datetime
import json
import os
import re
import tempfile
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, NamedTuple, Self

MARKER_ROOT_ENV = "CHAT_BACKGROUND_TASKS_DIR"
# The chat app's default data dir (``DEFAULT_CHAT_DATA_DIR`` in its ``config.py``) plus one segment.
DEFAULT_MARKER_ROOT = Path("data") / ".apps" / "chat" / "background_tasks"
MARKER_SUFFIX = ".json"

# What a chat id or task id may contribute to a path; anything else becomes ``_``.
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]")


class TaskSource(StrEnum):
    """Who recorded a task, which says whether the chat still hears of it once the agent's process is gone."""

    # A command ``run_in_background.py`` runs: its runner outlives the agent and reports how the command ended.
    RUN_IN_BACKGROUND = "run_in_background"
    # One of the harness's own background tasks: it ends with the agent's process, unreported.
    HARNESS = "harness"


class BackgroundTask(NamedTuple):
    """One pending task: what it is, when it started, and the pid that keeps it live."""

    source: TaskSource
    id: str
    description: str
    started_at: str
    pid: int
    # The harness that recorded it (``claude``); '' for the runner's.
    harness: str = ""
    # The harness's own type for the task; '' for the runner's.
    kind: str = ""
    command: str = ""
    pid_start: str = ""

    @property
    def file_name(self) -> str:
        return marker_file_name(self.source, self.id, self.harness)

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "source": self.source.value,
            "id": self.id,
            "description": self.description,
            "started_at": self.started_at,
            "pid": self.pid,
        }
        for key, value in (
            ("harness", self.harness),
            ("kind", self.kind),
            ("command", self.command),
            ("pid_start", self.pid_start),
        ):
            if value:
                data[key] = value
        return data

    @classmethod
    def from_json(cls, data: object) -> Self | None:
        """The task a marker's JSON holds, or None when it is not a well-formed marker."""
        fields = json_object(data)
        if fields is None:
            return None
        source = _task_source(fields.get("source"))
        task_id, pid = fields.get("id"), fields.get("pid")
        description, started_at = fields.get("description"), fields.get("started_at")
        if source is None or not (isinstance(task_id, str) and task_id):
            return None
        if not (isinstance(pid, int) and not isinstance(pid, bool) and pid > 0):
            return None
        if not (isinstance(description, str) and isinstance(started_at, str)):
            return None
        harness = _optional_text(fields, "harness")
        if (source is TaskSource.HARNESS) != bool(harness):
            return None
        return cls(
            source=source,
            id=task_id,
            description=description,
            started_at=started_at,
            pid=pid,
            harness=harness,
            kind=_optional_text(fields, "kind"),
            command=_optional_text(fields, "command"),
            pid_start=_optional_text(fields, "pid_start"),
        )


def json_object(value: object) -> dict[str, object] | None:
    """A parsed JSON value as an object with string keys, or None when it is not an object."""
    if not isinstance(value, dict):
        return None
    return {str(key): item for key, item in value.items()}


def _task_source(value: object) -> TaskSource | None:
    return next((source for source in TaskSource if source.value == value), None)


def _optional_text(fields: Mapping[str, object], key: str) -> str:
    value = fields.get(key, "")
    return value if isinstance(value, str) else ""


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def own_repo_root() -> Path:
    """The repo this package is in: the nearest ancestor holding ``system/scripts``, else cwd."""
    for ancestor in Path(__file__).resolve().parents:
        if (ancestor / "system" / "scripts").is_dir():
            return ancestor
    return Path.cwd()


def main_checkout(repo_root: Path) -> Path:
    """The checkout a git worktree belongs to, read from its ``.git`` file; ``repo_root`` itself
    when it is a main checkout or not a git checkout at all."""
    try:
        git_file = (repo_root / ".git").read_text(encoding="utf-8")
    except OSError:
        # No .git, or a directory: a main checkout or none.
        return repo_root
    git_dir_text = git_file.strip().removeprefix("gitdir:").strip()
    git_dir = (repo_root / git_dir_text).resolve()
    try:
        common_dir = (
            git_dir / (git_dir / "commondir").read_text(encoding="utf-8").strip()
        ).resolve()
    except OSError:
        return repo_root
    return common_dir.parent


def marker_root(environ: Mapping[str, str], repo_root: Path | None = None) -> Path:
    """The marker root of the checkout at ``repo_root`` (by default the one this package is in)."""
    override = environ.get(MARKER_ROOT_ENV, "")
    if override:
        return Path(override)
    return (
        main_checkout(own_repo_root() if repo_root is None else repo_root)
        / DEFAULT_MARKER_ROOT
    )


def own_chat_id(environ: Mapping[str, str]) -> str:
    """The caller's chat: ``MINDS_CHAT_ID`` on an agent the chat app created, else the agent's own id."""
    return environ.get("MINDS_CHAT_ID", "") or environ.get("MNGR_AGENT_ID", "")


def safe_name(value: str) -> str:
    return _UNSAFE_NAME_CHARS.sub("_", value)


def marker_file_name(source: TaskSource, task_id: str, harness: str = "") -> str:
    writer = (
        source.value
        if source is TaskSource.RUN_IN_BACKGROUND
        else f"{source.value}-{safe_name(harness)}"
    )
    return f"{writer}-{safe_name(task_id)}{MARKER_SUFFIX}"


def chat_dir(root: Path, chat_id: str) -> Path:
    return root / safe_name(chat_id)


def write_marker(root: Path, chat_id: str, task: BackgroundTask) -> None:
    """Write the marker atomically: a reader sees the old file, the new one, or none, never half of one."""
    directory = chat_dir(root, chat_id)
    directory.mkdir(parents=True, exist_ok=True)
    # A dot name with another suffix, so no reader takes the temp file for a marker.
    handle, temp_name = tempfile.mkstemp(dir=directory, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as temp_file:
            json.dump(task.to_json(), temp_file)
        os.replace(temp_name, directory / task.file_name)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def remove_marker(
    root: Path, chat_id: str, source: TaskSource, task_id: str, harness: str = ""
) -> None:
    (chat_dir(root, chat_id) / marker_file_name(source, task_id, harness)).unlink(
        missing_ok=True
    )


def is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Alive, and another user's.
        return True
    except OverflowError:
        # Past the C int range: no process has this pid.
        return False
    return True


def process_start_time(pid: int) -> str:
    """When the process started, in clock ticks since boot (``/proc/<pid>/stat`` field 22), or ''
    where ``/proc`` does not have it (macOS, or a process already gone)."""
    try:
        # The command name is arbitrary bytes; the numeric fields after it are ASCII.
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    # Fields after the command name, which is parenthesized and may hold spaces; field 22 is the 20th.
    fields = stat.rpartition(")")[2].split()
    return fields[19] if len(fields) > 19 else ""


def is_task_live(task: BackgroundTask) -> bool:
    """Whether the task's process still runs: its pid is alive and, when the marker recorded a
    start time and ``/proc`` reports one, it is the same process, not a later one given its pid."""
    if not is_pid_alive(task.pid):
        return False
    if not task.pid_start:
        return True
    return process_start_time(task.pid) in ("", task.pid_start)


def read_markers(directory: Path) -> list[BackgroundTask]:
    """Every well-formed marker in one chat's directory, live or stale; a missing directory holds none."""
    try:
        paths = sorted(directory.glob(f"*{MARKER_SUFFIX}"))
    except OSError:
        return []
    tasks: list[BackgroundTask] = []
    for path in paths:
        if path.name.startswith("."):
            continue
        try:
            task = BackgroundTask.from_json(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError):
            # Removed mid-read, or not JSON: not a marker either way.
            continue
        if task is not None:
            tasks.append(task)
    return tasks


def live_tasks_in(directory: Path) -> list[BackgroundTask]:
    """The markers in one chat's directory whose pid is alive, oldest first."""
    live = [task for task in read_markers(directory) if is_task_live(task)]
    return sorted(live, key=lambda task: (task.started_at, task.source.value, task.id))


def list_live_tasks(root: Path, chat_id: str) -> list[BackgroundTask]:
    return live_tasks_in(chat_dir(root, chat_id))


def is_busy(root: Path, chat_id: str) -> bool:
    """Whether a live task will wake the chat (the files alone; a turn in flight is the chat app's to know)."""
    return bool(list_live_tasks(root, chat_id))


def live_tasks_by_chat(root: Path) -> dict[str, list[BackgroundTask]]:
    """Every chat directory under the root that holds a live task, keyed by its directory name."""
    try:
        directories = sorted(path for path in root.iterdir() if path.is_dir())
    except OSError:
        return {}
    by_chat: dict[str, list[BackgroundTask]] = {}
    for directory in directories:
        tasks = live_tasks_in(directory)
        if tasks:
            by_chat[directory.name] = tasks
    return by_chat

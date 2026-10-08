#!/usr/bin/env python3
"""The background tasks a chat is waiting on, and whether the chat is busy.

A chat is busy when its agent will resume on its own: a turn is in flight, or a pending task's
completion will start one. A permission prompt is not busy, and neither is a process that will
never wake the agent. The pending tasks are one JSON file each, under the marker root::

    data/.apps/chat/background_tasks/<chat-id>/<source>-<task-id>.json

keyed by chat, not agent, so a handoff moves nothing: the successor reads the same directory.
Two writers, each owning its own files and never reading another's:

- ``run_in_background.py``, whose detached runner writes a ``run_in_background-*`` marker
  before the caller's tool call returns and removes it once the report is delivered or given up;
- Claude's Stop hook (``record-claude-stop``), which copies the ``background_tasks`` list Claude
  Code hands it into ``claude-*`` markers, keeping a still-listed task's start time and removing
  the ones no longer listed; and its SessionStart hook (``clear-claude``, on startup and resume),
  which removes them all, since a new Claude process has none of the old one's tasks.

Each marker names the pid whose death makes it stale (the runner, or the Claude main process),
and where ``/proc`` has it, that process's start time, so a pid recycled after a restart does not
bring a marker back. Readers skip a stale marker and never delete it; its writer's next pass does.

The chat app reads the directories on its state poller and is the authority for everyone else:
``list`` and ``is-busy`` ask it first (``GET /api/agents?tracked=true``), and read the files
themselves only when it cannot be reached or predates the fields. Then agent names are unknown
and a chat's active agent is taken to be the agent whose id is the chat id::

    python3 system/scripts/background_tasks.py list [--chat <chat-id>] [--format json|tsv]
    python3 system/scripts/background_tasks.py is-busy <chat-id>    # exit 0 busy, 1 not

``list`` prints every chat with pending tasks; ``tsv`` is one line per chat,
``chat_id<TAB>active_agent_id<TAB>active_agent_name<TAB>is_busy<TAB>task_count``. Studio reads
these columns by position, so a new one may only be appended. ``is_busy`` from the chat app
counts a turn in flight; the files alone cannot see one.

The marker root is ``data/.apps/chat/background_tasks`` under the workspace's main checkout:
the repo this script is in, or, when that is a git worktree (a worker's work dir), the checkout
the worktree belongs to, whose ``data/`` is the one the chat app reads.
``$MINDS_BACKGROUND_TASKS_DIR`` overrides it.

Standard library only: skills run it as ``python3 system/scripts/...``, the hooks run it before
any venv exists, and the chat app, the shell's avatar, the memory-candidate scan and the worker
idle check import it by path.
"""

import argparse
import datetime
import http.client
import importlib.util
import json
import os
import re
import sys
import tempfile
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

MARKER_ROOT_ENV = "MINDS_BACKGROUND_TASKS_DIR"
# The chat app's default data dir (``DEFAULT_CHAT_DATA_DIR`` in its ``config.py``) plus one segment.
DEFAULT_MARKER_ROOT = Path("data") / ".apps" / "chat" / "background_tasks"
MARKER_SUFFIX = ".json"

SOURCE_RUN_IN_BACKGROUND = "run_in_background"
SOURCE_CLAUDE = "claude"
# The ``type`` labels of Claude Code's Stop-hook ``background_tasks`` entries recorded as markers.
CLAUDE_KINDS = ("shell", "monitor", "workflow", "subagent")

# What a chat id or task id may contribute to a path; anything else becomes ``_``.
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]")

# The chat app's own agent list, with no fresh discovery; an older app ignores the query and
# answers without the busy fields, which the files then answer for.
AGENTS_PATH = "/api/agents?tracked=true"
CONNECT_TIMEOUT_SECONDS = 3.0
READ_TIMEOUT_SECONDS = 10.0

EXIT_BUSY = 0
EXIT_NOT_BUSY = 1


@dataclass(frozen=True)
class BackgroundTask:
    """One pending task: what it is, when it started, and the pid that keeps it live."""

    source: str
    id: str
    description: str
    started_at: str
    pid: int
    kind: str = ""
    command: str = ""
    pid_start: str = ""

    @property
    def file_name(self) -> str:
        return marker_file_name(self.source, self.id)

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "source": self.source,
            "id": self.id,
            "description": self.description,
            "started_at": self.started_at,
            "pid": self.pid,
        }
        if self.kind:
            data["kind"] = self.kind
        if self.command:
            data["command"] = self.command
        if self.pid_start:
            data["pid_start"] = self.pid_start
        return data

    @classmethod
    def from_json(cls, data: object) -> "BackgroundTask | None":
        """The task a marker's JSON holds, or None when it is not a well-formed marker."""
        if not isinstance(data, dict):
            return None
        source, task_id, pid = data.get("source"), data.get("id"), data.get("pid")
        description, started_at = data.get("description"), data.get("started_at")
        if not (
            isinstance(source, str) and source and isinstance(task_id, str) and task_id
        ):
            return None
        if not (isinstance(pid, int) and not isinstance(pid, bool) and pid > 0):
            return None
        if not (isinstance(description, str) and isinstance(started_at, str)):
            return None
        kind, command = data.get("kind", ""), data.get("command", "")
        pid_start = data.get("pid_start", "")
        return cls(
            source=source,
            id=task_id,
            description=description,
            started_at=started_at,
            pid=pid,
            kind=kind if isinstance(kind, str) else "",
            command=command if isinstance(command, str) else "",
            pid_start=pid_start if isinstance(pid_start, str) else "",
        )


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def script_repo_root() -> Path:
    """The repo this script is in: the nearest ancestor holding ``system/scripts``, else cwd."""
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


def marker_root(environ: Mapping[str, str]) -> Path:
    override = environ.get(MARKER_ROOT_ENV, "")
    if override:
        return Path(override)
    return main_checkout(script_repo_root()) / DEFAULT_MARKER_ROOT


def own_chat_id(environ: Mapping[str, str]) -> str:
    """The caller's chat: ``MINDS_CHAT_ID`` on an agent the chat app created, else the agent's own id."""
    return environ.get("MINDS_CHAT_ID", "") or environ.get("MNGR_AGENT_ID", "")


def safe_name(value: str) -> str:
    return _UNSAFE_NAME_CHARS.sub("_", value)


def marker_file_name(source: str, task_id: str) -> str:
    return f"{source}-{safe_name(task_id)}{MARKER_SUFFIX}"


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
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def remove_marker(root: Path, chat_id: str, source: str, task_id: str) -> None:
    (chat_dir(root, chat_id) / marker_file_name(source, task_id)).unlink(
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
    return True


def process_start_time(pid: int) -> str:
    """When the process started, in clock ticks since boot (``/proc/<pid>/stat`` field 22), or ''
    where ``/proc`` does not have it (macOS, or a process already gone)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
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
    current_start = process_start_time(task.pid)
    return current_start in ("", task.pid_start)


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
    return sorted(live, key=lambda task: (task.started_at, task.source, task.id))


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


@dataclass(frozen=True)
class ChatTasks:
    """One chat's busy verdict and pending tasks, as the CLI reads and prints them."""

    chat_id: str
    active_agent_id: str
    active_agent_name: str
    is_busy: bool
    tasks: tuple[BackgroundTask, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "active_agent_id": self.active_agent_id,
            "active_agent_name": self.active_agent_name,
            "is_busy": self.is_busy,
            "background_tasks": [task.to_json() for task in self.tasks],
        }

    def to_tsv(self) -> str:
        fields = (
            self.chat_id,
            self.active_agent_id,
            self.active_agent_name,
            "true" if self.is_busy else "false",
            str(len(self.tasks)),
        )
        return "\t".join(" ".join(field.split()) for field in fields)


class ChatAppUnavailableError(Exception):
    """The chat app could not answer, or answered without the busy fields, so the files decide."""


def _message_chat_module() -> Any:
    """``message_chat.py`` beside this file, loaded by path so an importer of this module needs no
    ``sys.path`` entry for ``system/scripts``."""
    spec = importlib.util.spec_from_file_location(
        "_message_chat_for_background_tasks",
        Path(__file__).resolve().parent / "message_chat.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def get_json(base_url: str, path: str) -> object:
    parsed = urllib.parse.urlsplit(base_url)
    connection = http.client.HTTPConnection(
        parsed.hostname or "127.0.0.1",
        parsed.port or 80,
        timeout=CONNECT_TIMEOUT_SECONDS,
    )
    try:
        connection.connect()
        if connection.sock is not None:
            connection.sock.settimeout(READ_TIMEOUT_SECONDS)
        connection.request("GET", path)
        response = connection.getresponse()
        raw = response.read()
    except (OSError, http.client.HTTPException) as exc:
        raise ChatAppUnavailableError(
            f"could not reach the chat app at {base_url}: {exc}"
        ) from exc
    finally:
        connection.close()
    if response.status != 200:
        raise ChatAppUnavailableError(
            f"the chat app answered HTTP {response.status} for {path}"
        )
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise ChatAppUnavailableError(
            f"the chat app's {path} answer is not JSON"
        ) from exc


def chats_from_agents_payload(payload: object) -> list[ChatTasks]:
    """Every agent's chat in a ``GET /api/agents`` answer, with its busy verdict and pending tasks.

    Raises ``ChatAppUnavailableError`` for an answer without the busy fields (a chat app from
    before them), which the files then answer for.
    """
    agents = payload.get("agents") if isinstance(payload, dict) else None
    if not isinstance(agents, list):
        raise ChatAppUnavailableError("the chat app's agent list has no agents")
    chats: list[ChatTasks] = []
    for agent in agents:
        if (
            not isinstance(agent, dict)
            or "is_busy" not in agent
            or "background_tasks" not in agent
        ):
            raise ChatAppUnavailableError(
                "the chat app's agent list predates the busy fields"
            )
        raw_tasks = agent["background_tasks"]
        tasks = tuple(
            task
            for task in (
                BackgroundTask.from_json(raw)
                for raw in (raw_tasks if isinstance(raw_tasks, list) else [])
            )
            if task is not None
        )
        agent_id = str(agent.get("id") or "")
        chats.append(
            ChatTasks(
                chat_id=str(agent.get("chat_id") or agent_id),
                active_agent_id=agent_id,
                active_agent_name=str(agent.get("name") or ""),
                is_busy=bool(agent["is_busy"]),
                tasks=tasks,
            )
        )
    return chats


def chats_from_files(root: Path) -> list[ChatTasks]:
    return [
        ChatTasks(
            chat_id=chat_id,
            active_agent_id=chat_id,
            active_agent_name="",
            is_busy=True,
            tasks=tuple(tasks),
        )
        for chat_id, tasks in live_tasks_by_chat(root).items()
    ]


def known_chats(environ: Mapping[str, str], cwd: Path) -> tuple[list[ChatTasks], str]:
    """The chats the chat app reports, or those the files hold live tasks for, and which answered
    (``chat_app`` or ``files``)."""
    base_url = _message_chat_module().chat_app_url(environ, cwd)
    try:
        return chats_from_agents_payload(get_json(base_url, AGENTS_PATH)), "chat_app"
    except ChatAppUnavailableError as exc:
        print(f"background_tasks.py: reading the marker files ({exc})", file=sys.stderr)
        return chats_from_files(marker_root(environ)), "files"


def cmd_list(
    chat_filter: str | None, output_format: str, environ: Mapping[str, str], cwd: Path
) -> int:
    chats, source = known_chats(environ, cwd)
    chats = [
        chat for chat in chats if chat.tasks and chat_filter in (None, chat.chat_id)
    ]
    if output_format == "tsv":
        for chat in chats:
            print(chat.to_tsv())
    else:
        print(
            json.dumps(
                {"source": source, "chats": [chat.to_json() for chat in chats]},
                indent=2,
            )
        )
    return 0


def cmd_is_busy(chat_id: str, environ: Mapping[str, str], cwd: Path) -> int:
    chats, _ = known_chats(environ, cwd)
    return (
        EXIT_BUSY
        if any(chat.chat_id == chat_id and chat.is_busy for chat in chats)
        else EXIT_NOT_BUSY
    )


def claude_hook_chat_id(environ: Mapping[str, str]) -> str:
    """The chat whose Claude markers this hook may write, or '' when it must do nothing.

    Only the agent's main Claude session writes: not mngr's subagent-proxy children, not a
    Claude outside an mngr agent, and not a nested Claude started in another directory. A
    nested Claude in the work dir passes these checks; the main session's next Stop corrects
    what it wrote. A Claude that does not name its own pid (``CLAUDE_PID``) writes nothing
    either: its markers would have no process to go stale by.
    """
    if environ.get("MNGR_CLAUDE_SUBAGENT_PROXY_CHILD") or not environ.get(
        "MAIN_CLAUDE_SESSION_ID"
    ):
        return ""
    raw_pid = environ.get("CLAUDE_PID", "")
    if not (raw_pid.isdigit() and int(raw_pid) > 0):
        return ""
    project_dir, work_dir = (
        environ.get("CLAUDE_PROJECT_DIR", ""),
        environ.get("MNGR_AGENT_WORK_DIR", ""),
    )
    if (
        not project_dir
        or not work_dir
        or Path(project_dir).resolve() != Path(work_dir).resolve()
    ):
        return ""
    return own_chat_id(environ)


def claude_main_pid(environ: Mapping[str, str]) -> int:
    """The Claude process whose exit makes its markers stale: ``CLAUDE_PID``, which Claude Code sets
    for its hooks (this hook's parent is only the shell it runs the hook command through)."""
    return int(environ["CLAUDE_PID"])


def claude_tasks_from_stop_input(
    payload: object,
    pid: int,
    pid_start: str,
    existing: Mapping[str, BackgroundTask],
    started_at: str,
) -> list[BackgroundTask] | None:
    """The Claude markers a Stop input calls for, or None when it carries no task list to copy.

    A task already marked keeps its start time.
    """
    entries = payload.get("background_tasks") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return None
    tasks: list[BackgroundTask] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        task_id, kind = entry.get("id"), entry.get("type")
        if not isinstance(task_id, str) or not task_id or kind not in CLAUDE_KINDS:
            continue
        description = entry.get("description")
        command = entry.get("command")
        previous = existing.get(task_id)
        tasks.append(
            BackgroundTask(
                source=SOURCE_CLAUDE,
                id=task_id,
                description=description if isinstance(description, str) else "",
                started_at=previous.started_at if previous is not None else started_at,
                pid=pid,
                kind=str(kind),
                command=command if isinstance(command, str) else "",
                pid_start=pid_start,
            )
        )
    return tasks


def record_claude_stop(root: Path, chat_id: str, payload: object, pid: int) -> None:
    """Make the chat's Claude markers match the Stop input's task list: write each listed task, and
    remove every Claude marker no longer listed."""
    directory = chat_dir(root, chat_id)
    existing = {
        task.id: task
        for task in read_markers(directory)
        if task.source == SOURCE_CLAUDE
    }
    tasks = claude_tasks_from_stop_input(
        payload, pid, process_start_time(pid), existing, now_iso()
    )
    if tasks is None:
        return
    for task in tasks:
        if existing.get(task.id) != task:
            write_marker(root, chat_id, task)
    listed = {task.id for task in tasks}
    for task_id in existing.keys() - listed:
        remove_marker(root, chat_id, SOURCE_CLAUDE, task_id)


def clear_claude(root: Path, chat_id: str) -> None:
    for task in read_markers(chat_dir(root, chat_id)):
        if task.source == SOURCE_CLAUDE:
            remove_marker(root, chat_id, SOURCE_CLAUDE, task.id)


def cmd_record_claude_stop(stdin: IO[str], environ: Mapping[str, str]) -> int:
    """The Stop hook. Every handled condition exits 0, so the hook never blocks Claude's stop."""
    chat_id = claude_hook_chat_id(environ)
    if not chat_id:
        return 0
    try:
        payload = json.loads(stdin.read() or "null")
    except ValueError as exc:
        print(
            f"background_tasks.py: the Stop input is not JSON ({exc})", file=sys.stderr
        )
        return 0
    try:
        record_claude_stop(
            marker_root(environ), chat_id, payload, claude_main_pid(environ)
        )
    except OSError as exc:
        print(
            f"background_tasks.py: could not record the Claude tasks: {exc}",
            file=sys.stderr,
        )
    return 0


def cmd_clear_claude(environ: Mapping[str, str]) -> int:
    chat_id = claude_hook_chat_id(environ)
    if not chat_id:
        return 0
    try:
        clear_claude(marker_root(environ), chat_id)
    except OSError as exc:
        print(
            f"background_tasks.py: could not clear the Claude tasks: {exc}",
            file=sys.stderr,
        )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="The background tasks chats are waiting on, and whether a chat is busy."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    list_parser = commands.add_parser(
        "list", help="Print every chat with pending background tasks."
    )
    list_parser.add_argument("--chat", default=None, help="Only this chat id.")
    list_parser.add_argument("--format", choices=("json", "tsv"), default="json")
    is_busy_parser = commands.add_parser(
        "is-busy", help="Exit 0 when the chat is busy, 1 when it is not."
    )
    is_busy_parser.add_argument("chat_id")
    commands.add_parser(
        "record-claude-stop",
        help="Claude's Stop hook: copy the input's background tasks into markers.",
    )
    commands.add_parser(
        "clear-claude",
        help="Claude's SessionStart hook: remove the Claude markers.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    stdin: IO[str] | None = None,
) -> int:
    args = _build_parser().parse_args(argv)
    resolved_environ = os.environ if environ is None else environ
    if args.command == "list":
        return cmd_list(args.chat, args.format, resolved_environ, Path.cwd())
    if args.command == "is-busy":
        return cmd_is_busy(args.chat_id, resolved_environ, Path.cwd())
    if args.command == "record-claude-stop":
        return cmd_record_claude_stop(
            sys.stdin if stdin is None else stdin, resolved_environ
        )
    return cmd_clear_claude(resolved_environ)


if __name__ == "__main__":
    raise SystemExit(main())

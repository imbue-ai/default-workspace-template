#!/usr/bin/env python3
"""grok's Stop, PostToolUse and SessionStart hooks that record its own background tasks as markers.

    python3 system/scripts/grok_background_task_hooks.py stop
    python3 system/scripts/grok_background_task_hooks.py post-tool-use
    python3 system/scripts/grok_background_task_hooks.py session-start

``.grok/hooks/policy.json`` wires all three, and each reads grok's hook payload from stdin. grok
hands a top-level session's ``Stop`` that session's ``backgroundTasks``: its shells, monitors and
subagents still running, each of which starts a turn of the session when it ends. ``stop`` copies
them into ``harness-grok-*`` markers, keeping a still-listed task's start time and describing a
shell, which grok lists with no description, by its command, and removes the markers of the
session's tasks it no longer lists and those of a grok that is gone. Each session lists only its
own tasks, and ``/new`` opens another in the same process, so the session that marked each task
is kept beside the agent's other state (``OWNERS_FILE_NAME``).

A turn cut short (Ctrl+C, a failure) runs no Stop. A background subagent it started still starts
a turn when it ends, so ``post-tool-use`` marks one as soon as ``spawn_subagent`` reports it
running in the background; a shell or monitor of such a turn starts none, and is marked only once
a later Stop lists it. ``session-start`` removes the markers of a grok that is gone; a live grok's
stay, since its other sessions' tasks still run.

Every marker names the grok process, which mngr's own SessionStart hook records under the agent's
state dir. Only the agent's main grok writes: not a grok a grok tool started (``GROK_AGENT=1``),
and not a subagent's own session. Each command exits 0 on every condition it handles.

The markers are ``system/libs/chat_background_tasks``, which this script puts on ``sys.path``:
grok runs its hooks with the system ``python3``.
"""

import argparse
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import IO, NamedTuple

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "libs" / "chat_background_tasks" / "src"),
)

import agent_rewrite_bash_command
from chat_background_tasks.markers import (
    BackgroundTask,
    TaskSource,
    chat_dir,
    is_pid_alive,
    is_task_live,
    json_object,
    marker_root,
    now_iso,
    own_chat_id,
    process_start_time,
    read_markers,
    remove_marker,
    write_marker,
)

# The chat app's name for the harness (``HarnessType.GROK``), by which it removes the marker of a
# task the transcript reports over; the two must match.
GROK_HARNESS = "grok"
# The ``type`` labels of a Stop input's ``backgroundTasks`` entries.
GROK_KINDS = ("shell", "monitor", "subagent")
SUBAGENT_KIND = "subagent"
# Where mngr's SessionStart hook records the pid of the grok process, under the agent's state dir.
GROK_PID_RELATIVE_PATH = Path("plugin", "grok", "grok.pid")
# Beside it, where ``/proc`` has it: that process's start in clock ticks since boot.
GROK_START_TICKS_RELATIVE_PATH = Path("plugin", "grok", "grok.start_ticks")
# How much later than its pid record a process may have started and still be the recorded one.
START_TIME_TOLERANCE_SECONDS = 5.0
# Set to ``1`` for every grok tool, and so for a grok one starts; never for the main grok's hooks.
NESTED_GROK_ENV = "GROK_AGENT"
# Under the agent's state dir: ``{"pid": <grok pid>, "sessions": {<task id>: <session id>}}``.
OWNERS_FILE_NAME = "grok_background_task_owners.json"
OWNERS_LOCK_FILE_NAME = "grok_background_task_owners.lock"
SPAWN_SUBAGENT_TOOL_NAME = "spawn_subagent"
# How ``spawn_subagent``'s result starts for a subagent left running in the background.
BACKGROUND_SUBAGENT_RESULT_PREFIX = "Subagent started in background."
# The result's id line, e.g. ``subagent_id: 01a167bb-400a-78c1-b325-10fd7006ecf2``.
_SUBAGENT_ID_LINE = re.compile(
    r"^subagent_id: ([A-Za-z0-9][A-Za-z0-9._-]*)$", re.MULTILINE
)


class HookCaller(NamedTuple):
    """The chat a hook writes markers in, and the agent state dir mngr keeps grok's records in."""

    chat_id: str
    state_dir: Path


def hook_caller(environ: Mapping[str, str]) -> HookCaller | None:
    """The chat and agent a hook run with ``environ`` speaks for, or None when it must do nothing:
    outside an mngr agent, or in a grok that a grok tool started."""
    if environ.get(NESTED_GROK_ENV) == "1":
        return None
    state_dir, chat_id = environ.get("MNGR_AGENT_STATE_DIR", ""), own_chat_id(environ)
    if not state_dir or not chat_id:
        return None
    return HookCaller(chat_id=chat_id, state_dir=Path(state_dir))


def recorded_grok_pid(state_dir: Path) -> int | None:
    """The pid of the agent's grok process, or None when none was recorded or that process is gone.

    The records ``imbue.mngr_grok.api.read_grok_pid`` reads, read the same way: a hook's system
    ``python3`` cannot import it. A live process other than the recorded one holds a reused pid, so
    it counts as gone: with a start-ticks record, one with another start; without, one that started
    after the pid record was written.
    """
    pid_path = state_dir / GROK_PID_RELATIVE_PATH
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
        recorded_at = pid_path.stat().st_mtime
    except (OSError, ValueError):
        return None
    if pid <= 1:
        return None
    recorded_start_ticks = _recorded_start_ticks(state_dir)
    if recorded_start_ticks is not None:
        return pid if process_start_time(pid) == recorded_start_ticks else None
    if not is_pid_alive(pid):
        return None
    started_at = _process_started_at(pid)
    return pid if started_at is None or started_at <= recorded_at + START_TIME_TOLERANCE_SECONDS else None


def _recorded_start_ticks(state_dir: Path) -> str | None:
    try:
        ticks = (state_dir / GROK_START_TICKS_RELATIVE_PATH).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return ticks if ticks.isdigit() else None


def _process_started_at(pid: int) -> float | None:
    """A live process's start, in epoch seconds, from ``ps``; None when ``ps`` cannot say."""
    try:
        finished = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "LC_ALL": "C"},
        )
        return time.mktime(time.strptime(finished.stdout.strip(), "%a %b %d %H:%M:%S %Y"))
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def top_level_session_id(payload: object) -> str | None:
    """The session a hook input is from, or None when there is none or it is a subagent's own."""
    fields = json_object(payload)
    if fields is None or fields.get("subagentType"):
        return None
    session_id = fields.get("sessionId")
    return session_id if isinstance(session_id, str) and session_id else None


def is_grok_task(task: BackgroundTask) -> bool:
    return task.source is TaskSource.HARNESS and task.harness == GROK_HARNESS


def grok_task(
    task_id: str,
    kind: str,
    description: str,
    command: str,
    pid: int,
    pid_start: str,
    started_at: str,
) -> BackgroundTask:
    return BackgroundTask(
        source=TaskSource.HARNESS,
        id=task_id,
        description=description,
        started_at=started_at,
        pid=pid,
        harness=GROK_HARNESS,
        kind=kind,
        command=command,
        pid_start=pid_start,
    )


def tasks_from_stop_input(
    payload: object,
    pid: int,
    pid_start: str,
    existing: Mapping[str, BackgroundTask],
    started_at: str,
) -> list[BackgroundTask] | None:
    """The grok markers a Stop input calls for, or None when it carries no task list (the Stop grok
    fires as a session ends). A task already marked keeps its start time, and one listed with no
    description is described by its command."""
    fields = json_object(payload)
    entries = None if fields is None else fields.get("backgroundTasks")
    if not isinstance(entries, list):
        return None
    tasks: list[BackgroundTask] = []
    for raw_entry in entries:
        entry = json_object(raw_entry)
        if entry is None:
            continue
        task_id, kind = entry.get("id"), entry.get("type")
        if not isinstance(task_id, str) or not task_id or kind not in GROK_KINDS:
            continue
        description, command = entry.get("description"), entry.get("command")
        written_command = (
            agent_rewrite_bash_command.original_command(command)
            if isinstance(command, str)
            else ""
        )
        previous = existing.get(task_id)
        tasks.append(
            grok_task(
                task_id,
                str(kind),
                description
                if isinstance(description, str) and description
                else written_command,
                written_command,
                pid,
                pid_start,
                previous.started_at if previous is not None else started_at,
            )
        )
    return tasks


def background_subagent_task(
    payload: object, pid: int, pid_start: str, started_at: str
) -> BackgroundTask | None:
    """The marker a PostToolUse input calls for: a subagent ``spawn_subagent`` left running in the
    background, or None for any other call."""
    fields = json_object(payload)
    if fields is None or fields.get("toolName") != SPAWN_SUBAGENT_TOOL_NAME:
        return None
    result = json_object(fields.get("toolResult"))
    text = None if result is None else result.get("text")
    if not isinstance(text, str) or not text.startswith(
        BACKGROUND_SUBAGENT_RESULT_PREFIX
    ):
        return None
    match = _SUBAGENT_ID_LINE.search(text)
    if match is None:
        return None
    tool_input = json_object(fields.get("toolInput"))
    description = None if tool_input is None else tool_input.get("description")
    return grok_task(
        match.group(1),
        SUBAGENT_KIND,
        description if isinstance(description, str) else "",
        "",
        pid,
        pid_start,
        started_at,
    )


def read_owners(state_dir: Path, pid: int) -> dict[str, str]:
    """The session that marked each task of the grok at ``pid``, by task id; none for another grok's record."""
    try:
        data = json_object(
            json.loads((state_dir / OWNERS_FILE_NAME).read_text(encoding="utf-8"))
        )
    except (OSError, ValueError):
        return {}
    sessions = None if data is None else json_object(data.get("sessions"))
    if data is None or data.get("pid") != pid or sessions is None:
        return {}
    return {
        task_id: session_id
        for task_id, session_id in sessions.items()
        if isinstance(session_id, str)
    }


def write_owners(state_dir: Path, pid: int, owners: Mapping[str, str]) -> None:
    handle, temp_name = tempfile.mkstemp(
        dir=state_dir, prefix=f".{OWNERS_FILE_NAME}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as temp_file:
            json.dump({"pid": pid, "sessions": dict(owners)}, temp_file)
        os.replace(temp_name, state_dir / OWNERS_FILE_NAME)
    finally:
        Path(temp_name).unlink(missing_ok=True)


@contextlib.contextmanager
def owners_lock(state_dir: Path) -> Iterator[None]:
    """Hold the lock under which a hook reads and rewrites the owners and the markers: the hooks of
    two sessions can run at once."""
    with open(state_dir / OWNERS_LOCK_FILE_NAME, "a") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        yield


def record_stop(
    root: Path, caller: HookCaller, pid: int, session_id: str, payload: object
) -> None:
    """Make the session's markers match its Stop input's task list: write each listed task, and
    remove the session's markers it no longer lists and every grok marker whose grok is gone.
    Another live grok's markers, and the other sessions' of this one, stay."""
    with owners_lock(caller.state_dir):
        existing = {
            task.id: task
            for task in read_markers(chat_dir(root, caller.chat_id))
            if is_grok_task(task)
        }
        tasks = tasks_from_stop_input(
            payload, pid, process_start_time(pid), existing, now_iso()
        )
        if tasks is None:
            return
        listed = {task.id for task in tasks}
        owners = {
            task_id: owner
            for task_id, owner in read_owners(caller.state_dir, pid).items()
            if owner != session_id
        }
        owners.update(dict.fromkeys(listed, session_id))
        write_owners(caller.state_dir, pid, owners)
        for task in tasks:
            if existing.get(task.id) != task:
                write_marker(root, caller.chat_id, task)
        for task_id, task in existing.items():
            if task_id in listed:
                continue
            if not is_task_live(task) or (task.pid == pid and task_id not in owners):
                remove_grok_marker(root, caller.chat_id, task_id)


def record_post_tool_use(
    root: Path, caller: HookCaller, pid: int, session_id: str, payload: object
) -> None:
    """Mark the subagent a PostToolUse input reports left running in the background, if any."""
    task = background_subagent_task(payload, pid, process_start_time(pid), now_iso())
    if task is None:
        return
    with owners_lock(caller.state_dir):
        owners = read_owners(caller.state_dir, pid)
        owners[task.id] = session_id
        write_owners(caller.state_dir, pid, owners)
        write_marker(root, caller.chat_id, task)


def clear(root: Path, caller: HookCaller) -> None:
    """Remove the chat's grok markers whose grok is gone."""
    for task in read_markers(chat_dir(root, caller.chat_id)):
        if is_grok_task(task) and not is_task_live(task):
            remove_grok_marker(root, caller.chat_id, task.id)


def remove_grok_marker(root: Path, chat_id: str, task_id: str) -> None:
    remove_marker(root, chat_id, TaskSource.HARNESS, task_id, GROK_HARNESS)


def read_payload(stdin: IO[str], event: str) -> object:
    try:
        return json.loads(stdin.read() or "null")
    except ValueError as exc:
        print(
            f"grok_background_task_hooks.py: the {event} input is not JSON ({exc})",
            file=sys.stderr,
        )
        return None


def cmd_turn_event(stdin: IO[str], environ: Mapping[str, str], event: str) -> int:
    """The Stop or PostToolUse hook: record what the input says of the session's background tasks."""
    payload = read_payload(stdin, event)
    caller = hook_caller(environ)
    session_id = top_level_session_id(payload)
    if caller is None or session_id is None:
        return 0
    pid = recorded_grok_pid(caller.state_dir)
    if pid is None:
        return 0
    record = record_stop if event == "Stop" else record_post_tool_use
    try:
        record(marker_root(environ), caller, pid, session_id, payload)
    except OSError as exc:
        print(
            f"grok_background_task_hooks.py: could not record the grok tasks: {exc}",
            file=sys.stderr,
        )
    return 0


def cmd_session_start(stdin: IO[str], environ: Mapping[str, str]) -> int:
    read_payload(stdin, "SessionStart")
    caller = hook_caller(environ)
    if caller is None:
        return 0
    try:
        clear(marker_root(environ), caller)
    except OSError as exc:
        print(
            f"grok_background_task_hooks.py: could not clear the grok tasks: {exc}",
            file=sys.stderr,
        )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="grok's hooks that record its own background tasks as markers."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "stop", help="The Stop hook: copy the input's background tasks into markers."
    )
    commands.add_parser(
        "post-tool-use",
        help="The PostToolUse hook: mark a subagent started in the background.",
    )
    commands.add_parser(
        "session-start",
        help="The SessionStart hook: clear the markers of a grok that is gone.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    stdin: IO[str] | None = None,
) -> int:
    args = _build_parser().parse_args(argv)
    resolved_environ = os.environ if environ is None else environ
    resolved_stdin = sys.stdin if stdin is None else stdin
    if args.command == "stop":
        return cmd_turn_event(resolved_stdin, resolved_environ, "Stop")
    if args.command == "post-tool-use":
        return cmd_turn_event(resolved_stdin, resolved_environ, "PostToolUse")
    return cmd_session_start(resolved_stdin, resolved_environ)


if __name__ == "__main__":
    raise SystemExit(main())

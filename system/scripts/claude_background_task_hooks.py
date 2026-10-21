#!/usr/bin/env python3
"""Claude's Stop and SessionStart hooks that record its own background tasks as markers.

    python3 system/scripts/claude_background_task_hooks.py stop
    python3 system/scripts/claude_background_task_hooks.py session-start

``.claude/settings.json`` wires both, and each reads the hook's input from stdin. Claude Code hands
its Stop hook the session's ``background_tasks`` list. ``stop`` copies the ones still pending into
``harness-claude-*`` markers, keeping a still-listed task's start time and removing the ones no
longer listed; a task's command is recorded as the agent wrote it, without the prefix
``agent_rewrite_bash_command.py`` puts on every Bash command. ``session-start`` (startup and
resume) removes those of a Claude process that is gone; Claude Code itself tells a resumed session
which of its tasks the restart stopped. Both act only for the agent's main Claude, never a nested
one, and only on that Claude's own markers and dead ones: after a handoff the chat's other agent
may be a live Claude whose markers are its own. Each exits 0 on every condition it handles, so it
never blocks Claude's stop.

A turn the user interrupts, or one that fails, runs no Stop hook, so its finished tasks would read
as pending until the next Stop or Claude's exit; the chat app removes such a marker once the
transcript reports the task over.

The markers are ``system/libs/chat_background_tasks``, which this script puts on ``sys.path``: the
hooks run it with a bare ``python3``, before any venv exists.
"""

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
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

# The chat app's name for the harness (``HarnessType.CLAUDE``), by which it removes the marker of a
# task the transcript reports over; the two must match.
CLAUDE_HARNESS = "claude"
# The ``type`` labels of Claude Code's Stop-hook ``background_tasks`` entries recorded as markers.
CLAUDE_KINDS = ("shell", "monitor", "workflow", "subagent")
# The statuses Claude Code gives a task that is over; an entry with one is not recorded.
CLAUDE_FINISHED_STATUSES = ("completed", "failed", "killed")
# Where mngr records the pid of the claude process that owns an agent, under its state dir.
CLAUDE_MAIN_PID_FILE_NAME = "claude_main_pid"


class HookCaller(NamedTuple):
    """The chat a hook writes markers in, and the Claude process whose exit makes them stale."""

    chat_id: str
    pid: int


def hook_caller(environ: Mapping[str, str]) -> HookCaller | None:
    """The chat and Claude a hook run with ``environ`` speaks for, or None when it must do nothing.

    Only the agent's main Claude session writes: not mngr's subagent-proxy children, not a Claude
    outside an mngr agent, not a nested Claude started in another directory, and not one started in
    the work dir while the agent's main Claude (the one mngr records as owning the agent) is alive.
    A Claude that does not name its own pid (``CLAUDE_PID``, which Claude Code sets for its hooks,
    whose parent is only the shell running the hook command) writes nothing either: its markers
    would have no process to go stale by.
    """
    if environ.get("MNGR_CLAUDE_SUBAGENT_PROXY_CHILD") or not environ.get(
        "MAIN_CLAUDE_SESSION_ID"
    ):
        return None
    raw_pid = environ.get("CLAUDE_PID", "")
    if not (raw_pid.isdigit() and int(raw_pid) > 0):
        return None
    project_dir, work_dir = (
        environ.get("CLAUDE_PROJECT_DIR", ""),
        environ.get("MNGR_AGENT_WORK_DIR", ""),
    )
    if (
        not project_dir
        or not work_dir
        or Path(project_dir).resolve() != Path(work_dir).resolve()
    ):
        return None
    pid = int(raw_pid)
    chat_id = own_chat_id(environ)
    if not chat_id or is_shadowed_by_main_claude(environ, pid):
        return None
    return HookCaller(chat_id=chat_id, pid=pid)


def recorded_main_claude_pid(environ: Mapping[str, str]) -> int | None:
    """The pid mngr's SessionStart hook recorded as the agent's main claude, or None when there is none."""
    state_dir = environ.get("MNGR_AGENT_STATE_DIR", "")
    if not state_dir:
        return None
    try:
        raw = (
            (Path(state_dir) / CLAUDE_MAIN_PID_FILE_NAME)
            .read_text(encoding="utf-8")
            .strip()
        )
    except OSError:
        return None
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


def process_command_name(pid: int) -> str:
    """The process's executable name as ``ps`` reports it, or '' when it cannot say."""
    try:
        result = subprocess.run(
            ["ps", "-o", "comm=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip()


def is_shadowed_by_main_claude(environ: Mapping[str, str], claude_pid: int) -> bool:
    """Whether another live claude owns the agent, which makes this one a nested claude.

    The same rule as mngr's own hooks: a recorded owner that is dead, or whose pid now holds
    something other than claude, owns nothing, and no record at all lets the hook through.
    """
    main_pid = recorded_main_claude_pid(environ)
    return (
        main_pid is not None
        and main_pid != claude_pid
        and is_pid_alive(main_pid)
        and Path(process_command_name(main_pid)).name == "claude"
    )


def is_claude_task(task: BackgroundTask) -> bool:
    return task.source is TaskSource.HARNESS and task.harness == CLAUDE_HARNESS


def tasks_from_stop_input(
    payload: object,
    caller: HookCaller,
    pid_start: str,
    existing: Mapping[str, BackgroundTask],
    started_at: str,
) -> list[BackgroundTask] | None:
    """The Claude markers a Stop input calls for, or None when it carries no task list to copy.

    A task already marked keeps its start time.
    """
    fields = json_object(payload)
    entries = None if fields is None else fields.get("background_tasks")
    if not isinstance(entries, list):
        return None
    tasks: list[BackgroundTask] = []
    for raw_entry in entries:
        entry = json_object(raw_entry)
        if entry is None:
            continue
        task_id, kind = entry.get("id"), entry.get("type")
        if not isinstance(task_id, str) or not task_id or kind not in CLAUDE_KINDS:
            continue
        if entry.get("status") in CLAUDE_FINISHED_STATUSES:
            continue
        description, command = entry.get("description"), entry.get("command")
        previous = existing.get(task_id)
        tasks.append(
            BackgroundTask(
                source=TaskSource.HARNESS,
                id=task_id,
                description=description if isinstance(description, str) else "",
                started_at=previous.started_at if previous is not None else started_at,
                pid=caller.pid,
                harness=CLAUDE_HARNESS,
                kind=str(kind),
                command=agent_rewrite_bash_command.original_command(command)
                if isinstance(command, str)
                else "",
                pid_start=pid_start,
            )
        )
    return tasks


def is_marker_ours(task: BackgroundTask, pid: int) -> bool:
    """Whether a hook fired by the claude at ``pid`` may remove this Claude marker: its own, or one
    whose claude is gone. A handoff's other agent shares the chat, and its markers stay its own."""
    return task.pid == pid or not is_task_live(task)


def record_stop(root: Path, caller: HookCaller, payload: object) -> None:
    """Make the caller's markers in its chat match the Stop input's task list: write each listed
    task, and remove the ones no longer listed and those whose claude is gone."""
    existing = {
        task.id: task
        for task in read_markers(chat_dir(root, caller.chat_id))
        if is_claude_task(task)
    }
    tasks = tasks_from_stop_input(
        payload, caller, process_start_time(caller.pid), existing, now_iso()
    )
    if tasks is None:
        return
    for task in tasks:
        if existing.get(task.id) != task:
            write_marker(root, caller.chat_id, task)
    listed = {task.id for task in tasks}
    for task_id in existing.keys() - listed:
        if is_marker_ours(existing[task_id], caller.pid):
            remove_claude_marker(root, caller.chat_id, task_id)


def clear(root: Path, caller: HookCaller) -> None:
    """Remove the chat's Claude markers a new claude process cannot have, its own and dead ones."""
    for task in read_markers(chat_dir(root, caller.chat_id)):
        if is_claude_task(task) and is_marker_ours(task, caller.pid):
            remove_claude_marker(root, caller.chat_id, task.id)


def remove_claude_marker(root: Path, chat_id: str, task_id: str) -> None:
    remove_marker(root, chat_id, TaskSource.HARNESS, task_id, CLAUDE_HARNESS)


def read_payload(stdin: IO[str], event: str) -> object:
    try:
        return json.loads(stdin.read() or "null")
    except ValueError as exc:
        print(
            f"claude_background_task_hooks.py: the {event} input is not JSON ({exc})",
            file=sys.stderr,
        )
        return None


def cmd_stop(stdin: IO[str], environ: Mapping[str, str]) -> int:
    caller = hook_caller(environ)
    if caller is None:
        return 0
    payload = read_payload(stdin, "Stop")
    try:
        record_stop(marker_root(environ), caller, payload)
    except OSError as exc:
        print(
            f"claude_background_task_hooks.py: could not record the Claude tasks: {exc}",
            file=sys.stderr,
        )
    return 0


def cmd_session_start(stdin: IO[str], environ: Mapping[str, str]) -> int:
    caller = hook_caller(environ)
    if caller is None:
        return 0
    read_payload(stdin, "SessionStart")
    try:
        clear(marker_root(environ), caller)
    except OSError as exc:
        print(
            f"claude_background_task_hooks.py: could not clear the Claude tasks: {exc}",
            file=sys.stderr,
        )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Claude's hooks that record its own background tasks as markers."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "stop", help="The Stop hook: copy the input's background tasks into markers."
    )
    commands.add_parser(
        "session-start",
        help="The SessionStart hook: clear this Claude's markers and dead ones.",
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
        return cmd_stop(resolved_stdin, resolved_environ)
    return cmd_session_start(resolved_stdin, resolved_environ)


if __name__ == "__main__":
    raise SystemExit(main())
